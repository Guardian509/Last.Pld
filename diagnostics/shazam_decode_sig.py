"""Decode a Shazam signature blob into its header fields and peak lists."""
import binascii, struct, sys

raw = open(sys.argv[1], "rb").read()
u32 = lambda o: struct.unpack("<I", raw[o:o+4])[0]

print("total bytes:", len(raw))
names = ["magic1","crc32","len_after_48","magic2","void1","void2","void3",
         "shifted_rate","void4","void5","num_samples","fixed_007c"]
for i, n in enumerate(names):
    print("  [%02x] %-14s 0x%08x  %d" % (i*4, n, u32(i*4), u32(i*4)))
print("  [30] magic3         0x%08x" % u32(0x30))
print("  [34] len_repeat     %d" % u32(0x34))

print("crc32 over bytes[8:] = 0x%08x   (stored 0x%08x)  match=%s" % (
    binascii.crc32(raw[8:]) & 0xffffffff, u32(4),
    (binascii.crc32(raw[8:]) & 0xffffffff) == u32(4)))

pos = 0x38
total_peaks = 0
while pos + 8 <= len(raw):
    band_id = u32(pos); size = u32(pos+4)
    band = band_id - 0x60030040
    body = raw[pos+8:pos+8+size]
    print("\nband %d (id 0x%08x) size=%d bytes -> %d peaks if 5b" % (
        band, band_id, size, size/5.0))
    peaks, off, last = [], 0, 0
    while off < len(body):
        d = body[off]
        if d == 0xFF:
            last = struct.unpack("<I", body[off+1:off+5])[0]; off += 5; continue
        last += d
        mag = struct.unpack("<H", body[off+1:off+3])[0]
        cbin = struct.unpack("<H", body[off+3:off+5])[0]
        peaks.append((last, mag, cbin)); off += 5
    total_peaks += len(peaks)
    print("  peaks:", len(peaks))
    for p in peaks[:6]:
        print("    pass=%-6d mag=%-6d corrected_bin=%-6d  bin~%.2f" % (
            p[0], p[1], p[2], p[2]/64.0))
    pad = (4 - (size % 4)) % 4
    pos += 8 + size + pad
print("\ntotal peaks:", total_peaks)
