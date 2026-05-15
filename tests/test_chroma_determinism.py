"""Chroma embedding determinism canary.

Pins the (text -> vector) baseline for the chromadb image. If the image is
ever upgraded — even to a same-tag re-publish on Docker Hub — and the bundled
all-MiniLM-L6-v2 ONNX model changes, this test fails loudly. Failure means:
all stored embeddings under the prior image are in a different embedding
space than newly-computed vectors, and either a re-embed pass or a
deliberate baseline regeneration is required.

This test is the "lockstep" guard described in docker-compose.yml chromadb
service comment. See `design:local-first-junto-v0-mvp` §4.3.a + §10
remaining risk #4 (Chroma embedding determinism, closed 2026-05-15).

Baselines below were captured 2026-05-15 from the running production
chromadb at image manifest digest
sha256:70c20dbcb64edbfad111a87149e7828348510246d1bb67c727f6aea926d3db7a.
The vector is packed as a sequence of IEEE 754 little-endian 32-bit floats
(struct.pack("<f", x)) and SHA256'd. This is exact-match: any change to
any float in any of the 384 dimensions changes the hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import uuid

import chromadb
import pytest

CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8001"))

# (text, expected_dim, expected_vector_sha256)
CANONICAL_BASELINES: list[tuple[str, int, str]] = [
    (
        "the quick brown fox jumps over the lazy dog",
        384,
        "99f444db8f3fa1a55750e6ec9bc36cbede9b119e42dd297070435ba5c0c65845",
    ),
    (
        "junto memory determinism canary",
        384,
        "dcdc273c54ecc6e3d03a9594237e11bf7740e2de47ba7d97211100c233233bda",
    ),
    (
        "Phase 2 cutover gate: all-MiniLM-L6-v2 baseline",
        384,
        "41b37099b87de94c13778f8f04ec99f339ce29d1e9a67a1aabf84277f528ce4b",
    ),
]


def _vec_sha256(vec: list[float]) -> str:
    packed = b"".join(struct.pack("<f", float(x)) for x in vec)
    return hashlib.sha256(packed).hexdigest()


async def _chroma_ping() -> bool:
    try:
        c = await chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        await c.heartbeat()
        return True
    except Exception:
        return False


@pytest.mark.integration
def test_chroma_embedding_baselines_unchanged():
    """Each canonical text must hash to the captured baseline.

    Runs against the live chromadb at $CHROMA_HOST:$CHROMA_PORT. Skipped if
    not reachable so unit-test runs in CI without a chromadb sidecar don't
    fail. Add the sidecar to the integration job to enforce this test.
    """
    if not asyncio.run(_chroma_ping()):
        pytest.skip(
            f"chromadb not reachable at {CHROMA_HOST}:{CHROMA_PORT} — "
            "run with docker compose up -d chromadb"
        )

    async def _run() -> list[tuple[str, int, str]]:
        client = await chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        # Throwaway collection per test run; deleted at end.
        col_name = f"det_canary_{uuid.uuid4().hex[:8]}"
        col = await client.get_or_create_collection(col_name)
        try:
            texts = [b[0] for b in CANONICAL_BASELINES]
            ids = [f"c{i}" for i in range(len(texts))]
            await col.add(ids=ids, documents=texts)
            got = await col.get(ids=ids, include=["embeddings"])
            return [
                (
                    got["documents"][i] if got.get("documents") else texts[i],
                    len(got["embeddings"][i]),
                    _vec_sha256(list(map(float, got["embeddings"][i]))),
                )
                for i in range(len(texts))
            ]
        finally:
            try:
                await client.delete_collection(col_name)
            except Exception:
                pass

    results = asyncio.run(_run())

    failures = []
    for (text, exp_dim, exp_hash), (_, got_dim, got_hash) in zip(
        CANONICAL_BASELINES, results, strict=True
    ):
        if got_dim != exp_dim or got_hash != exp_hash:
            failures.append(
                f"\n  text: {text!r}\n"
                f"    expected: dim={exp_dim} sha256={exp_hash}\n"
                f"    got:      dim={got_dim} sha256={got_hash}"
            )

    assert not failures, (
        "Chroma embedding determinism canary FAILED. The image was upgraded "
        "(or the model otherwise changed); all stored embeddings are now in "
        "a different embedding space than newly-computed ones.\n"
        "If this was intentional: regenerate CANONICAL_BASELINES in this "
        "file and plan a re-embed pass before merging.\n"
        + "".join(failures)
    )


def test_vec_sha256_helper_stable():
    """Self-check: the hash helper must be order- and value-sensitive."""
    assert _vec_sha256([1.0, 2.0, 3.0]) != _vec_sha256([3.0, 2.0, 1.0])
    assert _vec_sha256([1.0]) != _vec_sha256([1.0000001])
    # Same input → same hash, idempotent.
    h = _vec_sha256([0.1, -0.2, 0.3])
    assert h == _vec_sha256([0.1, -0.2, 0.3])
