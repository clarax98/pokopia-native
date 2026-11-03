#!/usr/bin/env python3
"""Import nxnative shader_split output into the Switch 2 corpus/hostkey format.

Usage: mkw_native_import.py <split package> <shader_map.tsv> <corpus dir> <control dir>
"""
import hashlib, pathlib, struct, sys

STAGE = {"vertex": 0, "hull": 1, "domain": 2, "geometry": 3, "pixel": 4, "compute": 5}

def main():
    pkg, mapfile, corpus, ctl = map(pathlib.Path, sys.argv[1:5])
    corpus = corpus / "mkw-bnsh"
    corpus.mkdir(parents=True, exist_ok=True); ctl.mkdir(parents=True, exist_ok=True)
    rows = [l.split("\t") for l in mapfile.read_text().splitlines() if l and not l.startswith("#")]
    rows = rows[1:]  # column header
    out = ["# hostkey\tcorpuskey\tstage\tcode_len\tinsn_bytes\tgroup\tshared\tsource"]
    seen = set()
    for filename, index, stname, key, source in rows:
        if key in seen: continue
        seen.add(key)
        data = (pkg / filename).read_bytes()
        off = 0; wanted = int(index)
        for recno in range(wanted + 1):
            if data[off:off+8] != b"BTCDBTCD": raise ValueError(f"{filename}: invalid record at {off:#x}")
            nctl, ncode = struct.unpack_from("<2I", data, off + 0x28)
            code_at = off + 0x38; ctl_at = code_at + ncode; end = ctl_at + nctl
            if end > len(data): raise ValueError(f"{filename}: truncated record {recno}")
            off = end
        code, control = data[code_at:ctl_at], data[ctl_at:end]
        stage = STAGE[stname]; header = 0 if stage == 5 else 0x80
        if len(code) <= header: raise ValueError(f"{key}: empty code after {header:#x}-byte header")
        instructions = code[header:len(code) - (len(code)-header) % 16]
        corpuskey = hashlib.blake2b(instructions, digest_size=8).hexdigest()
        hostkey = hashlib.sha256(control[:0x20] + bytes(8) + control[0x28:0xd8] + bytes(8) + code).hexdigest()[:16]
        if hostkey != key: raise ValueError(f"{key}: host key mismatch; recomputed {hostkey}")
        hdrkey = hashlib.blake2b(code[:header], digest_size=8).hexdigest() if header else "0" * 16
        (corpus / f"{corpuskey}-stage{stage}-{hdrkey}.program.bin").write_bytes(code)
        (corpus / f"{corpuskey}.txt").write_text(f"stage {stage}\nprogram 0\nheader_bytes {header}\nsource_bytes {len(instructions)}\nsource {source}\n")
        (ctl / f"{hostkey}.ctl").write_bytes(control)
        group = "64 1 1" if stage == 5 else "0 0 0"
        out.append(f"{hostkey}\t{corpuskey}\t{stage}\t{len(code)}\t{len(instructions)}\t{group}\t0\t{source}")
    (ctl / "hostkeys.tsv").write_text("\n".join(out) + "\n")
    print(f"imported {len(out)-1} programs")

if __name__ == "__main__": main()
