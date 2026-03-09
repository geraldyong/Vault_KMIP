from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import hvac
from fastapi import HTTPException
from kmip import enums
from kmip.core import enums as core_enums
from kmip.core.factories.attributes import AttributeFactory
from kmip.pie.client import ProxyKmipClient
from kmip.pie.exceptions import KmipOperationFailure

from .state import STATE, utc_now


class KmipDemoService:
    """
    Demo service for showing how a KMIP client can talk to a Vault KMIP server.

    High-level architecture:
    1. A separate Vault bootstrap container initializes/unseals Vault and configures:
       - KMIP secrets engine
       - KMIP scope
       - KMIP role
       - root token file shared to backend runtime volume

    2. This backend then:
       - waits for the shared Vault token file
       - authenticates to Vault over HTTP API
       - asks Vault to generate a KMIP client TLS credential
       - writes a PyKMIP config file using that credential
       - uses ProxyKmipClient to perform KMIP operations

    3. Local in-memory state (STATE.groups) is only for demo visibility.
       The actual KMIP key objects live inside Vault's KMIP engine.
    """

    def __init__(self) -> None:
        # Vault HTTP API endpoint used by hvac.
        self.vault_addr = os.getenv("VAULT_ADDR", "http://vault:8200")

        # Shared token file written by the bootstrap container.
        # This should contain the real Vault root token (or another privileged token).
        self.vault_token_file = os.getenv("VAULT_TOKEN_FILE", "")
        self.vault_token = self._load_vault_token()

        # KMIP-related environment settings.
        self.vault_kmip_mount = os.getenv("VAULT_KMIP_MOUNT", "kmip")
        self.vault_kmip_host = os.getenv("VAULT_KMIP_HOST", "vault")
        self.vault_kmip_port = int(os.getenv("VAULT_KMIP_PORT", "5696"))
        self.scope = os.getenv("VAULT_KMIP_SCOPE", "demo-scope")
        self.role = os.getenv("VAULT_KMIP_ROLE", "demo-admin")

        # Runtime directory shared inside the backend container.
        # We store:
        # - generated KMIP TLS bundle
        # - CA cert file
        # - generated PyKMIP config file
        self.runtime_dir = Path(os.getenv("KMIP_RUNTIME_DIR", "/app/runtime"))
        self.bundle_path = self.runtime_dir / "client_bundle.pem"
        self.ca_path = self.runtime_dir / "ca.pem"
        self.pykmip_config_path = self.runtime_dir / "pykmip.conf"

        # hvac client for talking to Vault's HTTP API.
        self.vault = hvac.Client(url=self.vault_addr, token=self.vault_token)

        # PyKMIP helper factory for building KMIP attributes for locate/get operations.
        self.attribute_factory = AttributeFactory()

    def bootstrap(self) -> None:
        """
        Backend startup sequence.

        This backend does NOT initialize or unseal Vault itself.
        It assumes the external bootstrap container has already done that.

        What this method does:
        1. Ensure runtime dir exists
        2. Wait for the shared Vault token file
        3. Rebuild hvac client using that token
        4. Wait for Vault to be unsealed and reachable
        5. Verify authentication works
        6. Ask Vault to generate a KMIP client credential
        7. Write a PyKMIP config file pointing to that credential

        After this, KMIP operations can be performed over TLS using ProxyKmipClient.
        """
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self._wait_for_token_file()
        self.vault_token = self._load_vault_token()
        self.vault = hvac.Client(url=self.vault_addr, token=self.vault_token)

        self._wait_for_vault()
        self._assert_authenticated()
        self._generate_client_credential()
        self._write_pykmip_config()

        STATE.add_log(
            "INFO",
            "Bootstrap complete.",
            vault_addr=self.vault_addr,
            kmip_endpoint=f"{self.vault_kmip_host}:{self.vault_kmip_port}",
            scope=self.scope,
            role=self.role,
            bundle_path=str(self.bundle_path),
            ca_path=str(self.ca_path),
            pykmip_config=str(self.pykmip_config_path),
        )

    def _load_vault_token(self) -> str:
        """
        Load the Vault token.

        Preference order:
        1. VAULT_TOKEN_FILE
        2. VAULT_TOKEN env var

        Returns empty string if not found yet.
        """
        if self.vault_token_file:
            token_path = Path(self.vault_token_file)
            if token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
                if token:
                    return token

        token = os.getenv("VAULT_TOKEN", "").strip()
        if token:
            return token

        return ""

    def _wait_for_token_file(self, timeout_seconds: int = 60) -> None:
        """
        Wait for the bootstrap container to write the shared root token file.

        This avoids a race where backend starts before bootstrap is finished.
        """
        if not self.vault_token_file:
            return

        STATE.add_log("INFO", "Waiting for Vault token file.", path=self.vault_token_file)
        token_path = Path(self.vault_token_file)
        start = time.time()

        while time.time() - start < timeout_seconds:
            if token_path.exists():
                content = token_path.read_text(encoding="utf-8").strip()
                if content:
                    STATE.add_log("INFO", "Vault token file is present.", path=self.vault_token_file)
                    return
            time.sleep(1)

        raise RuntimeError(f"Vault token file did not become ready in time: {self.vault_token_file}")

    def _assert_authenticated(self) -> None:
        """
        Confirm the token loaded into hvac is valid.
        """
        if not self.vault.is_authenticated():
            raise RuntimeError("Vault authentication failed: invalid or missing token")
        STATE.add_log("INFO", "Vault token authentication succeeded.")

    def _wait_for_vault(self, timeout_seconds: int = 120) -> None:
        """
        Wait for Vault HTTP API to be reachable and unsealed.
        """
        STATE.add_log("INFO", "Waiting for Vault HTTP API to become ready.")
        start = time.time()
        last_error = None

        while time.time() - start < timeout_seconds:
            try:
                if self.vault.sys.is_sealed() is False:
                    STATE.add_log("INFO", "Vault is reachable and unsealed.")
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(2)

        raise RuntimeError(f"Vault did not become ready in time: {last_error}")

    def _api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """
        Low-level helper for Vault HTTP API calls.

        hvac adapter.request can return different shapes depending on adapter/version:
        - sometimes a dict
        - sometimes a response object

        This helper normalizes that into a dict.
        """
        adapter = self.vault.adapter
        response = adapter.request(method, f"/v1/{path}", **kwargs)

        if isinstance(response, dict):
            return response

        if hasattr(response, "json"):
            try:
                return response.json()
            except Exception:
                pass

        if hasattr(response, "text") and response.text:
            return {"raw_text": response.text}

        return {}

    def _generate_client_credential(self) -> None:
        """
        Ask Vault KMIP engine to generate a TLS client credential for the configured role.

        Important:
        - We request format='pem_bundle'
        - For Vault KMIP, the 'certificate' field already contains a PEM bundle
          suitable for client auth
        - We also persist a CA file separately for PyKMIP config

        Output files:
        - client_bundle.pem
        - ca.pem
        """
        STATE.add_log("INFO", "Generating KMIP TLS client credential from Vault.")

        response = self._api(
            "POST",
            f"{self.vault_kmip_mount}/scope/{self.scope}/role/{self.role}/credential/generate",
            json={"format": "pem_bundle"},
        )

        data = response.get("data", {})

        certificate_bundle = data.get("certificate", "")
        ca_chain = data.get("ca_chain", [])

        if not certificate_bundle or "BEGIN" not in certificate_bundle:
            raise RuntimeError(f"Unexpected credential response from Vault: {response}")

        # For pem_bundle, Vault returns a combined PEM bundle.
        bundle = certificate_bundle.strip() + "\n"

        if isinstance(ca_chain, list) and ca_chain:
            ca_pem = "\n".join(item.rstrip() for item in ca_chain if item).strip() + "\n"
        elif isinstance(ca_chain, str) and ca_chain.strip():
            ca_pem = ca_chain.strip() + "\n"
        else:
            # Fallback: use the bundle if CA chain is not separately provided.
            ca_pem = bundle

        self.bundle_path.write_text(bundle, encoding="utf-8")
        self.ca_path.write_text(ca_pem, encoding="utf-8")

        STATE.add_log(
            "INFO",
            "Saved generated KMIP client credential to runtime directory.",
            bundle_path=str(self.bundle_path),
            ca_path=str(self.ca_path),
        )

    def _write_pykmip_config(self) -> Path:
        """
        Generate a PyKMIP client config file.

        Notes:
        - PyKMIP expects 'config' to be the section name and 'config_file' to be the file path.
        - PROTOCOL_TLS is used instead of PROTOCOL_TLS_CLIENT because TLS_CLIENT can trigger
          hostname-check behavior that PyKMIP does not pass through correctly.
        """
        self.pykmip_config_path.write_text(
            "\n".join(
                [
                    "[client]",
                    f"host={self.vault_kmip_host}",
                    f"port={self.vault_kmip_port}",
                    f"certfile={self.bundle_path}",
                    f"keyfile={self.bundle_path}",
                    f"ca_certs={self.ca_path}",
                    "cert_reqs=CERT_REQUIRED",
                    "ssl_version=PROTOCOL_TLS",
                    "do_handshake_on_connect=True",
                    "suppress_ragged_eofs=True",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        STATE.add_log("INFO", "Wrote PyKMIP client config.", path=str(self.pykmip_config_path))
        return self.pykmip_config_path

    def _client(self) -> ProxyKmipClient:
        """
        Create a PyKMIP client bound to the generated config.

        KMIP version is pinned to 1.2 for compatibility with older PyKMIP behavior.
        """
        return ProxyKmipClient(
            config="client",
            config_file=str(self.pykmip_config_path),
            kmip_version=enums.KMIPVersion.KMIP_1_2,
        )

    def health(self) -> dict[str, Any]:
        """
        Health check strategy:
        1. Confirm Vault HTTP API is alive
        2. Open a KMIP client session
        3. Attempt a lightweight KMIP locate()

        We do NOT use client.query() because ProxyKmipClient does not expose that method.
        """
        try:
            vault_health = self.vault.sys.read_health_status(method="GET")

            with self._client() as client:
                located = client.locate()

            return {
                "vault_addr": self.vault_addr,
                "kmip_server": f"{self.vault_kmip_host}:{self.vault_kmip_port}",
                "scope": self.scope,
                "role": self.role,
                "vault_initialized": vault_health.get("initialized"),
                "vault_sealed": vault_health.get("sealed"),
                "kmip_connected": True,
                "locate_result_count": len(located) if located is not None else 0,
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def _key_algorithm(self, algorithm: str) -> enums.CryptographicAlgorithm:
        """
        Map frontend algorithm string to PyKMIP enum.
        """
        mapping = {
            "AES": enums.CryptographicAlgorithm.AES,
            "HMAC_SHA256": enums.CryptographicAlgorithm.HMAC_SHA256,
            "HMAC_SHA384": enums.CryptographicAlgorithm.HMAC_SHA384,
            "HMAC_SHA512": enums.CryptographicAlgorithm.HMAC_SHA512,
        }
        return mapping[algorithm]

    def _usage_masks(self, key_type: str, algorithm: str) -> list[enums.CryptographicUsageMask]:
        """
        Decide which KMIP usage masks to assign.

        Logic:
        - KEK is used as a wrapping / key-encryption key, so it gets wrap/unwrap
          plus encrypt/decrypt for demo flexibility.
        - AES DEKs get encrypt/decrypt.
        - HMAC-style keys get generate/verify MAC.
        """
        if key_type == "KEK":
            return [
                enums.CryptographicUsageMask.WRAP_KEY,
                enums.CryptographicUsageMask.UNWRAP_KEY,
                enums.CryptographicUsageMask.ENCRYPT,
                enums.CryptographicUsageMask.DECRYPT,
            ]
        if algorithm == "AES":
            return [
                enums.CryptographicUsageMask.ENCRYPT,
                enums.CryptographicUsageMask.DECRYPT,
            ]
        return [enums.CryptographicUsageMask.GENERATE_MAC, enums.CryptographicUsageMask.VERIFY_MAC]

    def _logical_name(self, group_name: str, key_name: str) -> str:
        """
        Naming convention used for KMIP objects stored in Vault.

        This is important because local memory only tracks the "current" objects.
        We use the logical name later with locate() to inspect what exists in Vault.
        """
        return f"group::{group_name}::{key_name}"

    def _locate_by_name(self, logical_name: str) -> list[str]:
        """
        Find KMIP symmetric keys by logical name.

        This lets the UI show what is present in Vault for a given local-memory group.
        """
        name_attr = self.attribute_factory.create_attribute(core_enums.AttributeType.NAME, logical_name)
        obj_type_attr = self.attribute_factory.create_attribute(
            core_enums.AttributeType.OBJECT_TYPE,
            core_enums.ObjectType.SYMMETRIC_KEY,
        )
        with self._client() as client:
            return client.locate(attributes=[name_attr, obj_type_attr])

    def _read_key_summary(self, uid: str) -> dict[str, Any]:
        """
        Read key attributes for UI display.

        Some KMIP servers may return attributes that older PyKMIP parsing paths
        do not fully understand, such as KEY_VALUE_PRESENT. In that case, we do
        not want the whole Vault Browser node to fail. Instead, we return a
        partial summary with the UID and the decode error.
        """
        try:
            with self._client() as client:
                uid_out, attrs = client.get_attributes(uid)

            names = []
            attr_map: dict[str, Any] = {}

            for attr in attrs:
                try:
                    key = getattr(attr.attribute_name, "value", str(attr.attribute_name))
                    val = getattr(attr.attribute_value, "value", str(attr.attribute_value))
                    attr_map[key] = val
                    if key == "Name":
                        names.append(val)
                except Exception as attr_exc:  # noqa: BLE001
                    attr_map[f"unparsed_attribute_{len(attr_map)+1}"] = str(attr_exc)

            return {
                "uid": uid_out,
                "names": names,
                "attributes": attr_map,
            }

        except Exception as exc:  # noqa: BLE001
            return {
                "uid": uid,
                "names": [],
                "attributes": {},
                "warning": "Attribute decoding incomplete",
                "error": str(exc),
            }

    def create_group(self, group_name: str, algorithm: str, key_length: int) -> dict[str, Any]:
        """
        Create a demo encryption group containing:
        - KEK
        - DEK1
        - DEK2

        Flow:
        1. Check local memory to avoid duplicate group names
        2. For each key:
           - generate a logical name
           - send KMIP Create
           - send KMIP Activate
        3. Store only the "current" key references in local memory
        4. Store KEK history separately so rekeys can be shown over time

        Important:
        - The real objects live in Vault KMIP
        - Local memory is only the demo's working state / UI model
        """
        with STATE.lock:
            if group_name in STATE.groups:
                raise HTTPException(status_code=409, detail=f"Group '{group_name}' already exists in local memory.")

        STATE.add_log(
            "INFO",
            "Creating encryption group.",
            group_name=group_name,
            algorithm=algorithm,
            key_length=key_length,
        )

        created: dict[str, dict[str, Any]] = {}
        try:
            with self._client() as client:
                for key_name in ("KEK", "DEK1", "DEK2"):
                    logical_name = self._logical_name(group_name, key_name)

                    STATE.add_log(
                        "INFO",
                        "Issuing KMIP Create request.",
                        group_name=group_name,
                        key_name=key_name,
                        logical_name=logical_name,
                    )

                    uid = client.create(
                        self._key_algorithm(algorithm),
                        key_length,
                        name=logical_name,
                        cryptographic_usage_mask=self._usage_masks(key_name, algorithm),
                    )

                    # Newly created keys are activated so they are immediately usable.
                    client.activate(uid)

                    created[key_name] = {
                        "uid": uid,
                        "logical_name": logical_name,
                        "created_at": utc_now(),
                        "version": 1,
                    }

                    STATE.add_log(
                        "INFO",
                        "KMIP Create + Activate completed.",
                        group_name=group_name,
                        key_name=key_name,
                        uid=uid,
                    )

            group = {
                "group_name": group_name,
                "algorithm": algorithm,
                "key_length": key_length,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "keys": created,
                # History only really matters for KEK because rekey rotates the KEK.
                "history": {"KEK": [created["KEK"]]},
            }

            with STATE.lock:
                STATE.groups[group_name] = group

            return group
        except Exception as exc:  # noqa: BLE001
            STATE.add_log("ERROR", "Create group failed.", group_name=group_name, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def delete_group(self, group_name: str) -> dict[str, Any]:
        """
        Delete a demo encryption group.

        Flow:
        1. Load the group from local memory
        2. Collect UIDs of the current keys plus old KEK versions from history
        3. For each UID:
           - try revoke
           - try destroy
        4. Remove the group from local memory

        Why both revoke and destroy?
        - Revoke marks the key as no longer valid for normal usage
        - Destroy removes it from further operational use
        - In demos, some objects may already be in a state where one step fails;
          we treat those as cleanup warnings rather than full failure
        """
        with STATE.lock:
            group = STATE.groups.get(group_name)

        if not group:
            raise HTTPException(status_code=404, detail=f"Group '{group_name}' not found in local memory.")

        STATE.add_log("INFO", "Deleting encryption group.", group_name=group_name)
        errors: list[str] = []

        ids_to_remove = [v["uid"] for v in group["keys"].values()]
        ids_to_remove.extend(
            [
                entry["uid"]
                for entry in group.get("history", {}).get("KEK", [])
                if entry["uid"] not in ids_to_remove
            ]
        )

        with self._client() as client:
            for uid in ids_to_remove:
                try:
                    STATE.add_log("INFO", "Revoking key before destroy.", uid=uid)
                    client.revoke(
                        enums.RevocationReasonCode.CESSATION_OF_OPERATION,
                        uid=uid,
                        revocation_message="demo delete",
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"revoke {uid}: {exc}")

                try:
                    STATE.add_log("INFO", "Destroying key via KMIP.", uid=uid)
                    client.destroy(uid)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"destroy {uid}: {exc}")

        with STATE.lock:
            del STATE.groups[group_name]

        if errors:
            STATE.add_log("WARN", "Delete completed with non-fatal cleanup warnings.", group_name=group_name, errors=errors)
        else:
            STATE.add_log("INFO", "Delete completed successfully.", group_name=group_name)

        return {"group_name": group_name, "cleanup_warnings": errors}

    def rekey_group(self, group_name: str, activation_offset_seconds: int = 0) -> dict[str, Any]:
        """
        Rekey only the KEK, not the DEKs.

        Flow:
        1. Read current group from local memory
        2. Identify current KEK UID
        3. Issue KMIP Rekey against that KEK
        4. Update local memory so the current KEK points to the new UID
        5. Append the new KEK to KEK history

        Note:
        In this Vault KMIP flow, the rekeyed object may already be Active.
        So we do NOT call Activate again after Rekey.
        """
        with STATE.lock:
            group = STATE.groups.get(group_name)

        if not group:
            raise HTTPException(status_code=404, detail=f"Group '{group_name}' not found in local memory.")

        current_kek = group["keys"]["KEK"]
        STATE.add_log(
            "INFO",
            "Rekeying KEK only.",
            group_name=group_name,
            old_uid=current_kek["uid"],
            offset=activation_offset_seconds,
        )

        try:
            with self._client() as client:
                new_uid = client.rekey(uid=current_kek["uid"], offset=activation_offset_seconds)

            new_kek = {
                "uid": new_uid,
                "logical_name": current_kek["logical_name"],
                "created_at": utc_now(),
                "version": current_kek["version"] + 1,
                "previous_uid": current_kek["uid"],
            }

            with STATE.lock:
                group["keys"]["KEK"] = new_kek
                group.setdefault("history", {}).setdefault("KEK", []).append(new_kek)
                group["updated_at"] = utc_now()
                STATE.groups[group_name] = group

            STATE.add_log(
                "INFO",
                "KMIP Rekey complete. Local memory now points to the new KEK UID.",
                group_name=group_name,
                old_uid=current_kek["uid"],
                new_uid=new_uid,
            )
            return group

        except KmipOperationFailure as exc:
            STATE.add_log("ERROR", "KMIP rekey failed.", group_name=group_name, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def vault_objects_view(self) -> list[dict[str, Any]]:
        """
        Build a UI-friendly view of what appears to exist in Vault.

        We use the logical names stored in local memory and then:
        - locate matching KMIP objects in Vault
        - fetch their attributes
        - return both current UID and all located objects

        This helps illustrate the difference between:
        - local application memory state
        - actual KMIP objects stored in Vault
        """
        view: list[dict[str, Any]] = []
        snapshot = STATE.snapshot()

        for group in snapshot["groups"].values():
            for key_name, key_meta in group["keys"].items():
                logical_name = key_meta["logical_name"]
                matches = self._locate_by_name(logical_name)
                obj_summaries = []

                for uid in matches:
                    try:
                        obj_summaries.append(self._read_key_summary(uid))
                    except Exception as exc:  # noqa: BLE001
                        obj_summaries.append({"uid": uid, "error": str(exc)})

                view.append(
                    {
                        "group_name": group["group_name"],
                        "key_name": key_name,
                        "logical_name": logical_name,
                        "current_uid": key_meta["uid"],
                        "located_objects": obj_summaries,
                    }
                )

        return view

    def vault_browser_tree(self) -> dict[str, Any]:
        """
        Build a tree-shaped model for the UI "Vault Browser" panel.

        This is a logical browser, not a literal Vault filesystem.
        It combines:
        - KMIP mount/scope/role context
        - local memory group model
        - KMIP object lookup results from Vault
        """
        snapshot = STATE.snapshot()

        groups_children = []

        for group in snapshot["groups"].values():
            key_nodes = []

            for key_name, key_meta in group["keys"].items():
                logical_name = key_meta["logical_name"]
                matches = self._locate_by_name(logical_name)

                version_nodes = []
                for uid in matches:
                    try:
                        summary = self._read_key_summary(uid)
                        version_nodes.append(
                            {
                                "name": uid,
                                "type": "key-version",
                                "uid": uid,
                                "logical_name": logical_name,
                                "attributes": summary.get("attributes", {}),
                                "names": summary.get("names", []),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        version_nodes.append(
                            {
                                "name": uid,
                                "type": "key-version",
                                "uid": uid,
                                "logical_name": logical_name,
                                "warning": "Object found, but some attributes could not be decoded",
                                "error": str(exc),
                            }
                        )

                key_nodes.append(
                    {
                        "name": key_name,
                        "type": "key-family",
                        "logical_name": logical_name,
                        "current_uid": key_meta["uid"],
                        "children": version_nodes,
                    }
                )

            groups_children.append(
                {
                    "name": group["group_name"],
                    "type": "group",
                    "algorithm": group["algorithm"],
                    "key_length": group["key_length"],
                    "created_at": group["created_at"],
                    "updated_at": group["updated_at"],
                    "children": key_nodes,
                }
            )

        return {
            "name": self.vault_kmip_mount,
            "type": "mount",
            "children": [
                {
                    "name": f"scope/{self.scope}",
                    "type": "scope",
                    "children": [
                        {
                            "name": f"role/{self.role}",
                            "type": "role",
                        },
                        {
                            "name": "groups",
                            "type": "folder",
                            "children": groups_children,
                        },
                    ],
                }
            ],
        }
