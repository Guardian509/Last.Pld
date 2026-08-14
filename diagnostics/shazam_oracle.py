"""Make a deterministic 16k mono WAV, then dump the signature shazamio builds."""
import asyncio, base64, math, struct, sys

PATH = "oracle_clip.wav"
RATE, SECONDS = 16000, 12

def gen():
    n = RATE * SECONDS
    vals = []
    seed = 12345
    for i in range(n):
        t = i / RATE
        # a few steady tones plus deterministic noise, so peaks land in every band
        v = (0.30 * math.sin(2*math.pi*440*t) +
             0.25 * math.sin(2*math.pi*1180*t) +
             0.20 * math.sin(2*math.pi*2600*t) +
             0.15 * math.sin(2*math.pi*5200*t))
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        v += 0.05 * ((seed / 0x3FFFFFFF) - 1.0)
        vals.append(max(-1.0, min(1.0, v)))
    data = b"".join(struct.pack("<h", int(round(v * 32767))) for v in vals)
    hdr = (b"RIFF" + struct.pack("<i", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<ihhiihh", 16, 1, 1, RATE, RATE*2, 2, 16) +
           b"data" + struct.pack("<i", len(data)))
    open(PATH, "wb").write(hdr + data)
    print("wrote %s: %d samples" % (PATH, len(vals)))

async def main():
    gen()
    from shazamio_core import Recognizer
    rec = Recognizer()
    sig = await rec.recognize_path(PATH)
    song = sig.signature
    uri = song.uri
    print("samples:", song.samples)
    print("timestamp:", song.timestamp)
    raw = base64.b64decode(uri.split(",", 1)[1])
    print("signature bytes:", len(raw))
    open("oracle_sig.bin", "wb").write(raw)
    for off in range(0, min(len(raw), 96), 16):
        chunk = raw[off:off+16]
        print("%04x  %s" % (off, " ".join("%02x" % b for b in chunk)))
    print("...")
    print("last 32:", " ".join("%02x" % b for b in raw[-32:]))

asyncio.run(main())
