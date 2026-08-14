"""Build a Shazam signature from 16 kHz mono PCM, and check it against the oracle.

INCOMPLETE - DOES NOT WORK YET. The container format, FFT parameters and
magnitude scaling in here are verified correct against shazamio's own output;
the peak *selection* is not. This currently emits ~100k peaks where the oracle
emits 1481. See shazam-signature-format.md for what is established, what is
still unknown, and the order to attack it in.

Kept in diagnostics/ so the reverse-engineering is not lost. Nothing in the app
imports it.

Usage:  sigmaker.py <16k mono wav>     (needs numpy; compares to oracle_sig.bin)
"""
import binascii, math, struct, sys
import numpy as np

N = 2048
HOP = 128
BANDS = [(34, 66, 0), (66, 186, 1), (186, 462, 2), (462, 926, 3)]
NEIGHBOURS = (-10, -7, -4, -3, 1, 2, 5, 8)
OTHER_FRAMES = (-53, -45, 165, 172, 179, 186, 193, 200, 214, 222)


def read_pcm16_mono(path):
    raw = open(path, "rb").read()
    pos, data, rate = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<i", raw[pos + 4:pos + 8])[0]
        body = pos + 8
        if size < 0 or body + size > len(raw):
            size = len(raw) - body
        if cid == b"fmt ":
            rate = struct.unpack("<i", raw[body + 4:body + 8])[0]
        elif cid == b"data":
            data = raw[body:body + size]
        pos = body + size + (size & 1)
    return np.frombuffer(data, dtype="<i2").astype(np.float64), rate


def generate_peaks(samples):
    """Returns list of (fft_pass, band, corrected_bin, magnitude)."""
    window = np.hanning(N)
    n_frames = (len(samples) - N) // HOP + 1

    # magnitudes per frame, then spread in frequency and backwards in time
    spread = np.zeros((n_frames, N // 2 + 1))
    for f in range(n_frames):
        frame = samples[f * HOP: f * HOP + N]
        spec = np.fft.rfft(frame * window)
        mag = (spec.real ** 2 + spec.imag ** 2) / (1 << 17)
        np.maximum(mag, 1e-10, out=mag)
        spread[f] = mag

    # frequency spreading: each bin takes the max of itself and the next two
    for i in range(N // 2 - 1):
        spread[:, i] = np.maximum.reduce([spread[:, i], spread[:, i + 1], spread[:, i + 2]])

    # time spreading: a frame's value propagates back to 1, 3 and 6 frames ago
    for f in range(n_frames):
        for back in (1, 3, 6):
            if f - back >= 0:
                np.maximum(spread[f - back], spread[f], out=spread[f - back])

    peaks = []
    for f in range(n_frames):
        i46, i49 = f - 46, f - 49
        if i46 < 0 or i49 < 0:
            continue
        cur, prev = spread[i46], spread[i49]
        for b in range(10, 1015):
            v = cur[b]
            if v < 1.0 / 64.0 or v < prev[b - 1]:
                continue
            best = 0.0
            for off in NEIGHBOURS:
                if 0 <= b + off < len(prev):
                    best = max(best, prev[b + off])
            if v <= best:
                continue
            for off in OTHER_FRAMES:
                j = f - 1 + off
                if 0 <= j < n_frames:
                    best = max(best, spread[j][b])
            if v <= best:
                continue

            band = None
            for lo, hi, idx in BANDS:
                if lo <= b < hi:
                    band = idx
                    break
            if band is None:
                continue

            mag = math.log(max(v, 1.0 / 64.0)) * 1477.3 + 6144.0
            before = math.log(max(cur[b - 1], 1.0 / 64.0)) * 1477.3 + 6144.0
            after = math.log(max(cur[b + 1], 1.0 / 64.0)) * 1477.3 + 6144.0
            var1 = mag * 2 - before - after
            var2 = (after - before) * 32 / var1 if var1 else 0.0
            corrected = int(b * 64 + var2)
            peaks.append((i46, band, corrected, int(mag)))
    return peaks


def serialise(peaks, sample_count, rate=16000):
    by_band = {}
    for fft_pass, band, cbin, mag in peaks:
        by_band.setdefault(band, []).append((fft_pass, mag, cbin))

    body = b""
    for band in sorted(by_band):
        chunk, last = b"", 0
        for fft_pass, mag, cbin in sorted(by_band[band]):
            delta = fft_pass - last
            if delta >= 255:
                chunk += b"\xff" + struct.pack("<I", fft_pass)
                delta = 0
            chunk += struct.pack("<BHH", delta, min(mag, 65535), min(cbin, 65535))
            last = fft_pass
        pad = (4 - len(chunk) % 4) % 4
        body += struct.pack("<II", 0x60030040 + band, len(chunk)) + chunk + b"\x00" * pad

    after48 = struct.pack("<II", 0x40000000, len(body) + 8) + body
    header = struct.pack("<IIII", 0xCAFE2580, 0, len(after48), 0x94119C00)
    header += struct.pack("<III", 0, 0, 0)
    header += struct.pack("<I", 3 << 27)                     # 16 kHz
    header += struct.pack("<II", 0, 0)
    header += struct.pack("<I", sample_count + int(rate * 0.24))
    header += struct.pack("<I", 0x007C0000)
    blob = header + after48
    crc = binascii.crc32(blob[8:]) & 0xFFFFFFFF
    return blob[:4] + struct.pack("<I", crc) + blob[8:]


if __name__ == "__main__":
    samples, rate = read_pcm16_mono(sys.argv[1] if len(sys.argv) > 1 else "oracle_clip.wav")
    samples = samples[:160000]
    peaks = generate_peaks(samples)
    print("peaks found:", len(peaks))
    counts = {}
    for _, band, _, _ in peaks:
        counts[band] = counts.get(band, 0) + 1
    print("per band:", dict(sorted(counts.items())))

    blob = serialise(peaks, len(samples))
    open("mine_sig.bin", "wb").write(blob)
    print("signature bytes:", len(blob))

    try:
        ref = open("oracle_sig.bin", "rb").read()
        print("oracle bytes  :", len(ref))
        print("identical     :", ref == blob)
        for off in range(0, min(len(ref), len(blob))):
            if ref[off] != blob[off]:
                print("first difference at 0x%04x: oracle=%02x mine=%02x" % (
                    off, ref[off], blob[off]))
                break
    except IOError:
        pass
