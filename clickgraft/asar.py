"""
clickgraft.asar — ASAR parsing, rebuilding, and post-condition verification.
Ported from working reference implementation repack.py.
Target: Python 3.9+ (Standard Library only)
"""

import hashlib
import json
import os
import struct


class AsarArchive:
    """ASAR format parser — header parse, 4-byte padding calculation, content base."""

    def __init__(self, asar_path):
        self.asar_path = asar_path
        with open(asar_path, "rb") as f:
            self.raw_bytes = f.read()

        magic, size_and_pad_plus8, size_and_pad_plus4, header_size = struct.unpack("<IIII", self.raw_bytes[:16])
        if magic != 4:
            raise ValueError(f"Invalid ASAR header magic: {magic} in {asar_path}")

        self.header_size = header_size
        self.pad = (4 - (header_size % 4)) % 4
        self.content_base = 16 + header_size + self.pad

        header_json_bytes = self.raw_bytes[16:16 + header_size]
        self.header = json.loads(header_json_bytes.decode("utf-8"))
        self.header_json_bytes = header_json_bytes

    def count_entries(self):
        packed = 0
        unpacked = 0

        def _walk(node):
            nonlocal packed, unpacked
            if isinstance(node, dict) and "files" in node:
                for child in node["files"].values():
                    _walk(child)
            elif isinstance(node, dict):
                if node.get("unpacked") is True:
                    unpacked += 1
                else:
                    packed += 1

        _walk(self.header)
        return packed, unpacked

    def read_file_content(self, node):
        if node.get("unpacked") is True:
            raise ValueError("Cannot read packed content for unpacked entry")
        offset = int(node["offset"])
        size = int(node["size"])
        start = self.content_base + offset
        return self.raw_bytes[start:start + size]

    def get_all_file_nodes(self):
        """Returns dict of relative path -> node dict."""
        result = {}

        def _walk(rel_path, node):
            if isinstance(node, dict) and "files" in node:
                for name, child in node["files"].items():
                    child_path = f"{rel_path}/{name}" if rel_path else name
                    _walk(child_path, child)
            elif isinstance(node, dict):
                result[rel_path] = node

        _walk("", self.header)
        return result


def patch_and_repack_asar(source_asar_path, target_asar_path, patch_engine, manifest, fake_version=False):
    """
    Rebuilds ASAR without extracting files to disk.
    Applies manifest patch operations via patch_engine.
    Enforces all post-conditions:
    - packed & unpacked entry counts match manifest
    - path-set equality and per-path unpacked-flag stability
    - unpatched entries are byte-identical to source
    - per-entry SHA-256 matches header integrity.hash
    - unpacked files exist under target app.asar.unpacked/ and hash-match header integrity
    - max(offset + size) == blob length (zero blob slack, no gaps)
    """
    archive = AsarArchive(source_asar_path)

    packed_count, unpacked_count = archive.count_entries()
    expected_packed = manifest["asar_entries"]["packed"]
    expected_unpacked = manifest["asar_entries"]["unpacked"]

    if packed_count != expected_packed or unpacked_count != expected_unpacked:
        raise ValueError(
            f"Source ASAR entry count mismatch: {packed_count} packed (expected {expected_packed}), "
            f"{unpacked_count} unpacked (expected {expected_unpacked})"
        )

    # Deep copy header for output
    new_header = json.loads(json.dumps(archive.header))
    new_content = bytearray()
    current_offset = 0

    patched_paths = set(patch_engine.get_patched_paths())
    unpatched_byte_checks = []

    def _process_node(rel_path, node):
        nonlocal current_offset, new_content
        if isinstance(node, dict) and "files" in node:
            for name, child in node["files"].items():
                child_path = f"{rel_path}/{name}" if rel_path else name
                _process_node(child_path, child)
        elif isinstance(node, dict):
            if node.get("unpacked") is True:
                # Unpacked entry: no offset, contributes no bytes to content blob
                return

            orig_content = archive.read_file_content(node)
            final_content = orig_content

            if rel_path in patched_paths:
                final_content = patch_engine.apply_patches_for_path(rel_path, orig_content)
            else:
                unpatched_byte_checks.append((rel_path, orig_content, current_offset, len(orig_content)))

            if fake_version and rel_path == "package.json":
                pkg = json.loads(final_content.decode("utf-8"))
                pkg["version"] = "99.99.999"
                final_content = json.dumps(pkg, indent=2).encode("utf-8")

            # Calculate integrity for entry
            sha256_hash = hashlib.sha256(final_content).hexdigest()
            block_size = 4 * 1024 * 1024
            blocks = []
            for i in range(0, len(final_content), block_size):
                chunk = final_content[i:i + block_size]
                blocks.append(hashlib.sha256(chunk).hexdigest())

            node["offset"] = str(current_offset)
            node["size"] = len(final_content)
            node["integrity"] = {
                "algorithm": "SHA256",
                "hash": sha256_hash,
                "blockSize": block_size,
                "blocks": blocks
            }

            new_content.extend(final_content)
            current_offset += len(final_content)

    _process_node("", new_header)

    # Compact header JSON
    header_json_bytes = json.dumps(new_header, separators=(",", ":")).encode("utf-8")
    header_size = len(header_json_bytes)
    pad = (4 - (header_size % 4)) % 4

    header_binary = (
        struct.pack("<IIII", 4, header_size + pad + 8, header_size + pad + 4, header_size)
        + header_json_bytes
        + (b"\x00" * pad)
    )

    os.makedirs(os.path.dirname(target_asar_path), exist_ok=True)
    with open(target_asar_path, "wb") as f:
        f.write(header_binary)
        f.write(new_content)

    # --- POST-CONDITION VERIFICATION BLOCK ---
    rebuilt = AsarArchive(target_asar_path)
    r_packed, r_unpacked = rebuilt.count_entries()

    # 1. Entry counts
    if r_packed != expected_packed or r_unpacked != expected_unpacked:
        raise ValueError(
            f"Post-condition FAILED: entry count mismatch in rebuilt ASAR. "
            f"Packed={r_packed} (expected {expected_packed}), Unpacked={r_unpacked} (expected {expected_unpacked})"
        )

    # 2. Path-set equality & Per-path unpacked-flag stability (D6)
    rebuilt_nodes = rebuilt.get_all_file_nodes()
    source_nodes = archive.get_all_file_nodes()
    if set(rebuilt_nodes.keys()) != set(source_nodes.keys()):
        raise ValueError("Post-condition FAILED: set of paths in rebuilt ASAR differs from source ASAR")

    for rel_path, src_node in source_nodes.items():
        reb_node = rebuilt_nodes[rel_path]
        src_unpacked = bool(src_node.get("unpacked"))
        reb_unpacked = bool(reb_node.get("unpacked"))
        if src_unpacked != reb_unpacked:
            raise ValueError(
                f"Post-condition FAILED: unpacked flag instability for path '{rel_path}': "
                f"source={src_unpacked} != rebuilt={reb_unpacked}"
            )

    # 3. Unpatched byte-identity
    for rel_path, orig_content, exp_offset, exp_size in unpatched_byte_checks:
        rebuilt_node = rebuilt_nodes[rel_path]
        rebuilt_content = rebuilt.read_file_content(rebuilt_node)
        if rebuilt_content != orig_content:
            raise ValueError(f"Post-condition FAILED: unpatched entry '{rel_path}' is not byte-identical to source")

    # 4. Integrity verification & Blob slack assertion
    max_end_offset = 0
    for rel_path, node in rebuilt_nodes.items():
        if node.get("unpacked") is True:
            continue
        content = rebuilt.read_file_content(node)
        calc_hash = hashlib.sha256(content).hexdigest()
        hdr_hash = node.get("integrity", {}).get("hash")
        if calc_hash != hdr_hash:
            raise ValueError(f"Post-condition FAILED: entry '{rel_path}' hash {calc_hash} != header hash {hdr_hash}")
        
        end_off = int(node["offset"]) + int(node["size"])
        if end_off > max_end_offset:
            max_end_offset = end_off

    blob_length = len(new_content)
    if max_end_offset != blob_length:
        raise ValueError(f"Post-condition FAILED: blob slack detected! max(offset+size)={max_end_offset} != blob length {blob_length}")

    # 5. Unpacked files disk presence & SHA-256 verification (D4)
    target_unpacked_dir = target_asar_path + ".unpacked"
    for rel_path, node in rebuilt_nodes.items():
        if node.get("unpacked") is True:
            unpacked_file_path = os.path.join(target_unpacked_dir, rel_path)
            if not os.path.exists(unpacked_file_path):
                raise ValueError(
                    f"Post-condition FAILED: unpacked file missing from disk under app.asar.unpacked/: '{rel_path}'"
                )
            with open(unpacked_file_path, "rb") as uf:
                file_bytes = uf.read()
            calc_uf_hash = hashlib.sha256(file_bytes).hexdigest()

            exp_uf_hash = node.get("integrity", {}).get("hash")
            if not exp_uf_hash and rel_path in source_nodes:
                src_unpacked_file = os.path.join(source_asar_path + ".unpacked", rel_path)
                if os.path.exists(src_unpacked_file):
                    with open(src_unpacked_file, "rb") as sf:
                        exp_uf_hash = hashlib.sha256(sf.read()).hexdigest()

            if exp_uf_hash and calc_uf_hash != exp_uf_hash:
                raise ValueError(
                    f"Post-condition FAILED: unpacked file '{rel_path}' hash mismatch on disk: "
                    f"{calc_uf_hash} != expected {exp_uf_hash}"
                )

    header_hash = hashlib.sha256(header_json_bytes).hexdigest()
    return header_hash
