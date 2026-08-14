// Deterministic resampler test - no audio hardware, no luck involved.
//
// Feeds 88.2 kHz stereo float32 containing a 1 kHz tone (must survive) and a
// 20 kHz tone (must be filtered out). At 16 kHz, 20 kHz folds down to
// |20000 - 16000| = 4 kHz, so a missing anti-alias filter shows up as a loud
// 4 kHz tone that was never in the music.
using System;
using System.IO;
using LastPld;

static class DspTest
{
    const int InRate = 88200;
    const int Seconds = 3;

    static int Main()
    {
        string synth = "dsp_synth.wav";
        WriteSynth(synth);

        Wav.Clip clip = Wav.ReadMono(synth);
        Console.WriteLine("read back: " + clip.Samples.Length + " frames @ " +
                          clip.SampleRate + " Hz");

        short[] pcm = Resampler.To16kMono(clip.Samples, clip.SampleRate);
        Console.WriteLine("resampled: " + pcm.Length + " samples, expected " +
                          (Seconds * 16000));

        double keep = Amplitude(pcm, 1000, 16000);
        double alias = Amplitude(pcm, 4000, 16000);
        double quiet = Amplitude(pcm, 6500, 16000);

        Console.WriteLine("1 kHz (must survive)      : " + keep.ToString("F5"));
        Console.WriteLine("4 kHz (20 kHz alias)      : " + alias.ToString("F5"));
        Console.WriteLine("6.5 kHz (empty reference) : " + quiet.ToString("F5"));

        bool survived = keep > 0.25;
        bool suppressed = alias < keep / 50.0;
        Console.WriteLine("RESULT: tone " + (survived ? "survived" : "LOST") +
                          ", alias " + (suppressed ? "suppressed" : "LEAKED"));
        return survived && suppressed ? 0 : 1;
    }

    static void WriteSynth(string path)
    {
        int frames = InRate * Seconds;
        using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write))
        using (var w = new BinaryWriter(fs))
        {
            int dataBytes = frames * 2 * 4;          // stereo, float32
            w.Write(new[] { 'R', 'I', 'F', 'F' });
            w.Write(36 + dataBytes);
            w.Write(new[] { 'W', 'A', 'V', 'E' });
            w.Write(new[] { 'f', 'm', 't', ' ' });
            w.Write(16);
            w.Write((short)3);                       // IEEE float
            w.Write((short)2);                       // stereo
            w.Write(InRate);
            w.Write(InRate * 8);
            w.Write((short)8);
            w.Write((short)32);
            w.Write(new[] { 'd', 'a', 't', 'a' });
            w.Write(dataBytes);

            for (int i = 0; i < frames; i++)
            {
                double t = (double)i / InRate;
                float v = (float)(0.5 * Math.Sin(2 * Math.PI * 1000 * t) +
                                  0.5 * Math.Sin(2 * Math.PI * 20000 * t));
                w.Write(v);                          // left
                w.Write(v);                          // right
            }
        }
    }

    // Goertzel: energy at one frequency, without a whole FFT.
    static double Amplitude(short[] x, double freq, int rate)
    {
        double w = 2.0 * Math.PI * freq / rate;
        double coeff = 2.0 * Math.Cos(w);
        double s1 = 0, s2 = 0;
        for (int i = 0; i < x.Length; i++)
        {
            double s0 = x[i] / 32768.0 + coeff * s1 - s2;
            s2 = s1;
            s1 = s0;
        }
        double power = s1 * s1 + s2 * s2 - coeff * s1 * s2;
        return 2.0 * Math.Sqrt(Math.Max(0, power)) / x.Length;
    }
}
