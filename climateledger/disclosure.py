from __future__ import annotations

from hashlib import sha256
from typing import Any

from .intake import canonical_bytes


SEALED_KEY = "sealed_sha256"
SENSITIVE_MANIFEST_FIELDS = ("quality_findings",)
PUBLIC_VIEW = "public_delivery"
INTERNAL_VIEW = "internal_audit"
VIEW_SCHEMA = 1

REDACTIONS = [
    {"path": "manifest.quality_findings", "disposition": "sealed",
     "detail": "quality findings stay as a SHA-256 commitment"},
    {"path": "batches.metadata", "disposition": "withheld",
     "detail": "internal contacts and processing notes stay in the ledger"},
    {"path": "batches.findings", "disposition": "withheld",
     "detail": "per-batch quality evidence stays in the ledger"},
    {"path": "batches.measurements", "disposition": "withheld",
     "detail": "raw values and conversion rules stay in the ledger"},
    {"path": "batches.events", "disposition": "withheld",
     "detail": "ingest, approve and publish events stay in the ledger"},
]


def is_sealed(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {SEALED_KEY}
            and isinstance(value[SEALED_KEY], str) and len(value[SEALED_KEY]) == 64)


def seal_value(value: Any) -> dict[str, str]:
    return {SEALED_KEY: sha256(canonical_bytes(value)).hexdigest()}


def seal_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(manifest)
    for field in SENSITIVE_MANIFEST_FIELDS:
        if field in sealed and not is_sealed(sealed[field]):
            sealed[field] = seal_value(sealed[field])
    return sealed


def manifest_digest(manifest: dict[str, Any]) -> str:
    return sha256(canonical_bytes(seal_manifest(manifest))).hexdigest()


def document_digest(document: dict[str, Any]) -> str:
    return sha256(canonical_bytes(document)).hexdigest()


class CredentialViews:
    """Builds the public delivery and internal audit views of a release.

    Both views derive from the same registered release digest, which is
    computed over the sealed manifest at publish time. Masking is a pure
    projection: it never rewrites the stored manifest or its digest.
    """

    def __init__(self, store, releases):
        self.store = store
        self.releases = releases

    def public_view(self, version: int) -> dict[str, Any]:
        bundle = self.releases.export(version)
        release = self.store.release(version)
        document = {"view": PUBLIC_VIEW, "view_schema": VIEW_SCHEMA,
                    "version": release["version"], "digest": release["digest"],
                    "created_at": release["created_at"],
                    "manifest": seal_manifest(bundle["manifest"]),
                    "entries": bundle["entries"],
                    "redactions": [dict(item) for item in REDACTIONS]}
        document["view_digest"] = document_digest(document)
        return document

    def internal_view(self, version: int) -> dict[str, Any]:
        bundle = self.releases.export(version)
        release = self.store.release(version)
        public = self.public_view(version)
        batches = [self._batch_chain(batch_id)
                   for batch_id in sorted({entry["batch_id"] for entry in bundle["entries"]})]
        entries_hash = sha256(canonical_bytes(bundle["entries"])).hexdigest()
        consistency = {"sealed_manifest_digest": manifest_digest(bundle["manifest"]),
                       "digest_matches": manifest_digest(bundle["manifest"]) == release["digest"],
                       "entries_sha256": entries_hash,
                       "entries_match": entries_hash == bundle["manifest"]["entries_sha256"],
                       "public_view_digest": public["view_digest"]}
        return {"view": INTERNAL_VIEW, "view_schema": VIEW_SCHEMA,
                "version": release["version"], "digest": release["digest"],
                "created_at": release["created_at"],
                "manifest": bundle["manifest"], "entries": bundle["entries"],
                "lineage": self.releases.lineage(version), "batches": batches,
                "sealed_fields": list(SENSITIVE_MANIFEST_FIELDS),
                "redactions": [dict(item) for item in REDACTIONS],
                "consistency": consistency}

    def _batch_chain(self, batch_id: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        return {"batch_id": batch["id"], "source": batch["source"],
                "external_id": batch["external_id"], "revision": batch["revision"],
                "status": batch["status"], "payload_hash": batch["payload_hash"],
                "row_count": batch["row_count"], "received_at": batch["received_at"],
                "approved_at": batch["approved_at"],
                "published_version": batch["published_version"],
                "metadata": batch["metadata"], "findings": batch["findings"],
                "measurements": self.store.measurements(batch_id),
                "events": self.store.events(f"batch:{batch_id}")}

    @staticmethod
    def verify_view(document: Any) -> bool:
        if not isinstance(document, dict):
            return False
        if document.get("view") == PUBLIC_VIEW:
            return CredentialViews.verify_public(document)
        if document.get("view") == INTERNAL_VIEW:
            return CredentialViews.verify_internal(document)
        return False

    @staticmethod
    def verify_public(document: Any) -> bool:
        if not isinstance(document, dict):
            return False
        try:
            if document.get("view") != PUBLIC_VIEW or document.get("view_schema") != VIEW_SCHEMA:
                return False
            manifest = document.get("manifest")
            entries = document.get("entries")
            digest = document.get("digest")
            view_digest = document.get("view_digest")
            if not isinstance(manifest, dict) or not isinstance(entries, list):
                return False
            if not isinstance(digest, str) or not isinstance(view_digest, str):
                return False
            if document.get("version") != manifest.get("version"):
                return False
            if manifest.get("entry_count") != len(entries):
                return False
            for field in SENSITIVE_MANIFEST_FIELDS:
                if field in manifest and not is_sealed(manifest[field]):
                    return False
            source_files = manifest.get("source_files")
            if not isinstance(source_files, list):
                return False
            if any(not isinstance(item, dict) for item in source_files):
                return False
            proven = {item.get("batch_id") for item in source_files}
            if any(not isinstance(entry, dict) or entry.get("batch_id") not in proven
                   for entry in entries):
                return False
            if sha256(canonical_bytes(entries)).hexdigest() != manifest.get("entries_sha256"):
                return False
            if manifest_digest(manifest) != digest:
                return False
            body = {key: value for key, value in document.items() if key != "view_digest"}
            return document_digest(body) == view_digest
        except (TypeError, ValueError):
            return False

    @staticmethod
    def verify_internal(document: Any) -> bool:
        if not isinstance(document, dict):
            return False
        try:
            if document.get("view") != INTERNAL_VIEW or document.get("view_schema") != VIEW_SCHEMA:
                return False
            manifest = document.get("manifest")
            entries = document.get("entries")
            digest = document.get("digest")
            if not isinstance(manifest, dict) or not isinstance(entries, list):
                return False
            if not isinstance(digest, str):
                return False
            if document.get("version") != manifest.get("version"):
                return False
            for field in SENSITIVE_MANIFEST_FIELDS:
                if field in manifest and is_sealed(manifest[field]):
                    return False
            if manifest.get("entry_count") != len(entries):
                return False
            if sha256(canonical_bytes(entries)).hexdigest() != manifest.get("entries_sha256"):
                return False
            if manifest_digest(manifest) != digest:
                return False
            consistency = document.get("consistency")
            if not isinstance(consistency, dict):
                return False
            if consistency.get("sealed_manifest_digest") != digest:
                return False
            if consistency.get("digest_matches") is not True:
                return False
            if consistency.get("entries_match") is not True:
                return False
            projection = {"view": PUBLIC_VIEW, "view_schema": VIEW_SCHEMA,
                          "version": document.get("version"), "digest": digest,
                          "created_at": document.get("created_at"),
                          "manifest": seal_manifest(manifest), "entries": entries,
                          "redactions": [dict(item) for item in REDACTIONS]}
            projection["view_digest"] = document_digest(projection)
            if consistency.get("public_view_digest") != projection["view_digest"]:
                return False
            source_files = manifest.get("source_files")
            batches = document.get("batches")
            if not isinstance(source_files, list) or not isinstance(batches, list):
                return False
            if any(not isinstance(item, dict) for item in source_files + batches):
                return False
            chain = {}
            for item in batches:
                batch_id = item.get("batch_id")
                if not isinstance(batch_id, int) or isinstance(batch_id, bool) or batch_id in chain:
                    return False
                chain[batch_id] = item
            for proof in source_files:
                batch = chain.get(proof.get("batch_id"))
                if batch is None:
                    return False
                if any(batch.get(key) != proof.get(key) for key in
                       ("source", "external_id", "revision", "payload_hash", "row_count")):
                    return False
            return True
        except (TypeError, ValueError):
            return False
