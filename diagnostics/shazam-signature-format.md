# Shazam signature format — what is verified, and what is not

Working notes toward removing the last external dependency from Identify, so
`Last.Pld.exe` needs nothing beside it. **Incomplete.** Read the "Not solved"
section before trusting any of this.

Everything below was derived empirically against the `shazamio-core` already
installed in `_build\venv312`, by feeding it a deterministic clip and reading
the bytes it produced. No GPL source was consulted or copied, which matters:
SongRec is GPLv3 and this project is MIT.

## Why bother

Identify currently needs a Python 3.12 venv beside the exe. Capture and
resampling are already in-process (see `Audio.cs`), so the recogniser is the
only thing left standing between Identify and a genuinely single-file app.

## Reproducing the oracle

`oracle.py` writes a deterministic 16 kHz mono clip — four steady tones at 440,
1180, 2600 and 5200 Hz plus a seeded LCG noise floor — then asks
`shazamio_core.Recognizer` for its signature and dumps the base64 out of
`sig.signature.uri`. Deterministic input means the bytes are comparable run to
run, and the known tone frequencies make the frequency mapping checkable by
hand.

`decode_sig.py` parses a signature blob back into header fields and peak lists.

Note the oracle uses a **10 second** segment (160000 samples) regardless of how
long the clip is.

## Container format — VERIFIED

All offsets little-endian. Header is 48 bytes, then an 8-byte preamble, then
band records.

| Offset | Value | Meaning |
|---|---|---|
| 0x00 | `0xcafe2580` | magic 1 |
| 0x04 | CRC32 | zlib CRC32 over `bytes[8:]`, verified matching |
| 0x08 | u32 | total length − 48 |
| 0x0c | `0x94119c00` | magic 2 |
| 0x10 | 0 | |
| 0x14 | 0 | |
| 0x18 | 0 | |
| 0x1c | `id << 27` | sample-rate id; 16 kHz is id **3** → `0x18000000` |
| 0x20 | 0 | |
| 0x24 | 0 | |
| 0x28 | u32 | sample count **+ `rate × 0.24`** (160000 → 163840) |
| 0x2c | `0x007c0000` | fixed |
| 0x30 | `0x40000000` | magic 3 |
| 0x34 | u32 | same length as 0x08 |
| 0x38.. | records | one per frequency band |

Each band record:

    u32 band_id      0x60030040 + band_index   (bands 0..3)
    u32 size         bytes of peak data
    ... peak data, then zero padding to a 4-byte boundary

Each peak is **5 bytes**:

    u8  delta        frames since the previous peak in this band
    u16 magnitude
    u16 corrected_bin

If the gap does not fit in a byte, emit `0xFF` followed by a u32 absolute frame
number, then continue.

## Analysis parameters — VERIFIED

- FFT size **2048**, hop **128**, **Hann** window, 16 kHz mono
- Samples fed in as **raw int16** values, *not* normalised to ±1
- `magnitude = (re² + im²) / (1 << 17)`
- `corrected_bin = bin × 64` plus a sub-bin correction

Bin width is 16000/2048 = 7.8125 Hz, and the oracle's output confirms it:

| Tone | Expected bin | Oracle `corrected_bin ÷ 64` |
|---|---|---|
| 440 Hz | 56.32 | 56.33 |
| 1180 Hz | 151.04 | 151.03 |

Stored magnitude — confirmed to within 2 units on a steady tone (computed
34168, oracle 34170):

    peak_magnitude = ln(magnitude) × 1477.3 + 6144

Sub-bin correction, believed right but not independently confirmed:

    var1 = mag × 2 − mag_before − mag_after
    var2 = (mag_after − mag_before) × 32 / var1
    corrected_bin = bin × 64 + var2

Band edges follow the documented 250 / 520 / 1450 / 3500 / 5500 Hz splits,
which in bins at this resolution are roughly 34 / 66 / 186 / 462 / 926.

## NOT solved — peak selection

This is where the work stopped. `sigmaker.py` implements everything above and
still produces the wrong peaks:

    oracle:  1481 peaks   band 0:36   1:248   2:593   3:604
    mine:  100506 peaks   before any time-locality throttle

A plain "must be a local maximum over ±W frames" throttle does not explain the
gap. At W=64 it still yields 12804 peaks, and the band shape is wrong — band 3
dominates at 6975 where the oracle has 604, roughly equal to band 2.

The strongest clue: a fixed threshold of `1/64` passes **100%** of candidates,
because magnitudes on raw int16 input are ~1e8, not ~1. So the real detector
almost certainly uses an **adaptive threshold** — a decaying per-bin floor, or
a running noise estimate — rather than a constant. That is the next thing to
work out.

## Does it need to be bit-exact?

Probably not. Shazam matches on peak constellations and tolerates extra peaks,
and the genuine tones *are* present in the current output, just buried in
spurious ones. So the decisive test is not a byte diff against the oracle, it
is whether Shazam's backend identifies a real song from a signature this code
produced.

**That test has not been run yet.** It needs real music playing during capture;
the attempt made so far captured silence (peak 0.00001, nothing was playing).

Suggested order when picking this back up:

1. Capture ~12 s of real music on a machine that is actually playing something.
2. POST the **oracle's** signature for that clip to Shazam and confirm a match —
   this proves the HTTP layer independently of the DSP.
3. POST the signature `sigmaker.py` produces for the same clip. A match means
   the peak selection is good enough and only needs tuning; no match means the
   detector is genuinely wrong and the adaptive threshold is worth chasing.

Keeping those two steps separate is the point — otherwise a failure cannot be
attributed to either the network call or the fingerprint.
