// Audio — reading what LoopCap captured, and getting it to the 16 kHz mono
// that fingerprinting wants. This is the half of Identify that used to be
// "shell out to ffmpeg", which meant Identify quietly required a separate
// install that the rest of the app did not.
//
// Compiled into Last.Pld.exe. Nothing here touches the network or the disk
// beyond the two temp files Identify already used.

using System;
using System.IO;

namespace LastPld
{
    static class Wav
    {
        public class Clip
        {
            public float[] Samples;      // mono, nominally -1..1
            public int SampleRate;
        }

        // LoopCap writes a canonical 44-byte header: 'fmt ' of 16 bytes, tag 1
        // (PCM) or 3 (IEEE float), then 'data'. Chunks are still walked rather
        // than assumed, because a device could hand back an extended header and
        // silently shifting by a few bytes would turn music into noise.
        public static Clip ReadMono(string path)
        {
            byte[] raw = File.ReadAllBytes(path);
            if (raw.Length < 44 || Tag(raw, 0) != "RIFF" || Tag(raw, 8) != "WAVE")
                throw new InvalidDataException("not a WAV file");

            int channels = 0, sampleRate = 0, bits = 0, formatTag = 0;
            int dataStart = -1, dataLength = 0;

            int pos = 12;
            while (pos + 8 <= raw.Length)
            {
                string id = Tag(raw, pos);
                int size = BitConverter.ToInt32(raw, pos + 4);
                int body = pos + 8;
                if (size < 0 || body + size > raw.Length) size = raw.Length - body;

                if (id == "fmt " && size >= 16)
                {
                    formatTag = BitConverter.ToInt16(raw, body);
                    channels = BitConverter.ToInt16(raw, body + 2);
                    sampleRate = BitConverter.ToInt32(raw, body + 4);
                    bits = BitConverter.ToInt16(raw, body + 14);
                }
                else if (id == "data")
                {
                    dataStart = body;
                    dataLength = size;
                }

                pos = body + size + (size & 1);      // chunks are word aligned
            }

            if (dataStart < 0 || channels <= 0 || sampleRate <= 0)
                throw new InvalidDataException("WAV is missing fmt or data");

            bool isFloat = formatTag == 3;
            int bytesPerSample = bits / 8;
            if (bytesPerSample <= 0) throw new InvalidDataException("bad bit depth");

            int frames = dataLength / (bytesPerSample * channels);
            var mono = new float[frames];

            for (int f = 0; f < frames; f++)
            {
                double sum = 0;
                int at = dataStart + f * bytesPerSample * channels;
                for (int c = 0; c < channels; c++)
                {
                    int o = at + c * bytesPerSample;
                    if (isFloat && bits == 32) sum += BitConverter.ToSingle(raw, o);
                    else if (isFloat && bits == 64) sum += BitConverter.ToDouble(raw, o);
                    else if (bits == 16) sum += BitConverter.ToInt16(raw, o) / 32768.0;
                    else if (bits == 32) sum += BitConverter.ToInt32(raw, o) / 2147483648.0;
                    else if (bits == 8) sum += (raw[o] - 128) / 128.0;
                    else throw new InvalidDataException("unsupported bit depth " + bits);
                }
                mono[f] = (float)(sum / channels);
            }

            return new Clip { Samples = mono, SampleRate = sampleRate };
        }

        public static void WritePcm16(string path, short[] samples, int sampleRate)
        {
            using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write))
            using (var w = new BinaryWriter(fs))
            {
                int dataBytes = samples.Length * 2;
                w.Write(new[] { 'R', 'I', 'F', 'F' });
                w.Write(36 + dataBytes);
                w.Write(new[] { 'W', 'A', 'V', 'E' });
                w.Write(new[] { 'f', 'm', 't', ' ' });
                w.Write(16);
                w.Write((short)1);                  // PCM
                w.Write((short)1);                  // mono
                w.Write(sampleRate);
                w.Write(sampleRate * 2);            // byte rate
                w.Write((short)2);                  // block align
                w.Write((short)16);                 // bits
                w.Write(new[] { 'd', 'a', 't', 'a' });
                w.Write(dataBytes);
                foreach (short s in samples) w.Write(s);
            }
        }

        static string Tag(byte[] b, int at)
        {
            if (at + 4 > b.Length) return "";
            return "" + (char)b[at] + (char)b[at + 1] + (char)b[at + 2] + (char)b[at + 3];
        }
    }

    static class Resampler
    {
        // Windowed-sinc resampling. Downsampling 88.2 kHz to 16 kHz throws away
        // most of the band, so the anti-alias filter is not optional: without it
        // everything above 8 kHz folds back down as noise, and the fingerprint
        // is computed from exactly that region.
        //
        // The kernel is a Blackman-windowed sinc, widened as the rate drops so
        // the filter keeps the same number of zero crossings either way.
        const int ZeroCrossings = 16;

        public static short[] To16kMono(float[] input, int inputRate)
        {
            const int outputRate = 16000;
            if (input == null || input.Length == 0) return new short[0];

            double ratio = (double)outputRate / inputRate;
            float[] resampled = ratio == 1.0 ? input : Resample(input, ratio);

            var pcm = new short[resampled.Length];
            for (int i = 0; i < resampled.Length; i++)
            {
                double v = resampled[i] * 32767.0;
                if (v > 32767.0) v = 32767.0;
                else if (v < -32768.0) v = -32768.0;
                pcm[i] = (short)Math.Round(v);
            }
            return pcm;
        }

        static float[] Resample(float[] input, double ratio)
        {
            // Cutoff in cycles per input sample. When downsampling it has to sit
            // at the output Nyquist, not the input's.
            double cutoff = 0.5 * Math.Min(1.0, ratio);
            double halfWidth = ZeroCrossings / (2.0 * cutoff);

            int outLength = (int)(input.Length * ratio);
            var output = new float[outLength];

            for (int n = 0; n < outLength; n++)
            {
                double center = n / ratio;
                int first = (int)Math.Ceiling(center - halfWidth);
                int last = (int)Math.Floor(center + halfWidth);
                if (first < 0) first = 0;
                if (last > input.Length - 1) last = input.Length - 1;

                double sum = 0, weight = 0;
                for (int i = first; i <= last; i++)
                {
                    double t = center - i;
                    double w = Blackman(t / halfWidth);
                    double k = Sinc(2.0 * cutoff * t) * 2.0 * cutoff * w;
                    sum += input[i] * k;
                    weight += k;
                }

                // Normalising by the realised kernel weight keeps the level flat
                // at the very start and end, where the window is truncated.
                output[n] = (float)(weight > 1e-9 ? sum / weight : 0.0);
            }
            return output;
        }

        static double Sinc(double x)
        {
            if (Math.Abs(x) < 1e-9) return 1.0;
            double px = Math.PI * x;
            return Math.Sin(px) / px;
        }

        static double Blackman(double x)
        {
            if (x < -1.0 || x > 1.0) return 0.0;
            double t = Math.PI * (x + 1.0) / 2.0 * 2.0;   // map -1..1 onto 0..2pi
            return 0.42 - 0.5 * Math.Cos(t) + 0.08 * Math.Cos(2.0 * t);
        }
    }
}
