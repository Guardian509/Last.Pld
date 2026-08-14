// LoopCap — records what is coming OUT of the default playback device
// (WASAPI loopback) to a WAV file. This is the capture half of song
// identification: reels and other sources publish no metadata, so the only
// way to name the track is to fingerprint the audio itself.
//
// Standalone on purpose, so the capture path can be verified before it is
// wired into Last.Pld.
//
// Build:
//   csc.exe /target:exe /out:loopcap.exe LoopCap.cs
// Usage:
//   loopcap.exe <seconds> <out.wav>

using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

static class LoopCap
{
    // ---- COM plumbing -------------------------------------------------

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    class MMDeviceEnumerator { }

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator
    {
        int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
        int GetDevice(string id, out IMMDevice device);
        int RegisterEndpointNotificationCallback(IntPtr client);
        int UnregisterEndpointNotificationCallback(IntPtr client);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice
    {
        int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
                     [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        int OpenPropertyStore(int access, out IntPtr store);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        int GetState(out int state);
    }

    // Method order below IS the vtable layout — do not reorder.
    [ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        int Initialize(int shareMode, int streamFlags, long bufferDuration,
                       long periodicity, IntPtr format, IntPtr sessionGuid);
        int GetBufferSize(out uint bufferFrameCount);
        int GetStreamLatency(out long latency);
        int GetCurrentPadding(out uint padding);
        int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closestMatch);
        int GetMixFormat(out IntPtr deviceFormat);
        int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
        int Start();
        int Stop();
        int Reset();
        int SetEventHandle(IntPtr handle);
        int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    [ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        int GetBuffer(out IntPtr data, out uint numFrames, out uint flags,
                      out long devicePosition, out long qpcPosition);
        int ReleaseBuffer(uint numFrames);
        int GetNextPacketSize(out uint numFrames);
    }

    const int eRender = 0;
    const int eConsole = 0;
    const int ShareModeShared = 0;
    const int StreamFlagsLoopback = 0x00020000;
    const uint BufferFlagsSilent = 0x2;
    const int ClsCtxAll = 23;

    // ---- entry point --------------------------------------------------

    [MTAThread]
    static int Main(string[] args)
    {
        int seconds = 10;
        string outPath = "capture.wav";
        if (args.Length > 0) int.TryParse(args[0], out seconds);
        if (args.Length > 1) outPath = args[1];

        try
        {
            Capture(seconds, outPath);
            Console.WriteLine("OK " + outPath);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("FAIL " + ex.Message);
            return 1;
        }
    }

    static void Capture(int seconds, string outPath)
    {
        var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumerator());

        IMMDevice device;
        Check(enumerator.GetDefaultAudioEndpoint(eRender, eConsole, out device),
              "GetDefaultAudioEndpoint");

        var iidAudioClient = typeof(IAudioClient).GUID;
        object clientObj;
        Check(device.Activate(ref iidAudioClient, ClsCtxAll, IntPtr.Zero, out clientObj),
              "Activate(IAudioClient)");
        var client = (IAudioClient)clientObj;

        IntPtr fmt;
        Check(client.GetMixFormat(out fmt), "GetMixFormat");

        // WAVEFORMATEX prefix
        short formatTag = Marshal.ReadInt16(fmt, 0);
        short channels = Marshal.ReadInt16(fmt, 2);
        int sampleRate = Marshal.ReadInt32(fmt, 4);
        short blockAlign = Marshal.ReadInt16(fmt, 12);
        short bitsPerSample = Marshal.ReadInt16(fmt, 14);

        // 0xFFFE = WAVE_FORMAT_EXTENSIBLE; the real format sits in SubFormat,
        // but for our purposes float-vs-PCM follows from the bit depth.
        bool isFloat = (bitsPerSample == 32);

        Console.WriteLine(string.Format(
            "device mix: {0} Hz, {1} ch, {2}-bit, tag=0x{3:X4}{4}",
            sampleRate, channels, bitsPerSample, formatTag,
            isFloat ? " (float)" : ""));

        Check(client.Initialize(ShareModeShared, StreamFlagsLoopback,
                                10000000L, 0, fmt, IntPtr.Zero), "Initialize");

        var iidCapture = typeof(IAudioCaptureClient).GUID;
        object captureObj;
        Check(client.GetService(ref iidCapture, out captureObj), "GetService");
        var capture = (IAudioCaptureClient)captureObj;

        using (var fs = new FileStream(outPath, FileMode.Create, FileAccess.Write))
        {
            WriteWavHeader(fs, channels, sampleRate, bitsPerSample, isFloat, 0);

            long dataBytes = 0;
            Check(client.Start(), "Start");
            var deadline = DateTime.UtcNow.AddSeconds(seconds);
            var buffer = new byte[blockAlign * 4096];

            while (DateTime.UtcNow < deadline)
            {
                Thread.Sleep(10);

                uint packetFrames;
                Check(capture.GetNextPacketSize(out packetFrames), "GetNextPacketSize");

                while (packetFrames > 0)
                {
                    IntPtr data;
                    uint frames, flags;
                    long devPos, qpcPos;
                    Check(capture.GetBuffer(out data, out frames, out flags, out devPos, out qpcPos),
                          "GetBuffer");

                    int bytes = (int)frames * blockAlign;
                    if (bytes > buffer.Length) buffer = new byte[bytes];

                    if ((flags & BufferFlagsSilent) != 0)
                        Array.Clear(buffer, 0, bytes);      // nothing playing: real silence
                    else
                        Marshal.Copy(data, buffer, 0, bytes);

                    fs.Write(buffer, 0, bytes);
                    dataBytes += bytes;

                    Check(capture.ReleaseBuffer(frames), "ReleaseBuffer");
                    Check(capture.GetNextPacketSize(out packetFrames), "GetNextPacketSize");
                }
            }

            Check(client.Stop(), "Stop");

            // backfill the sizes now that they are known
            fs.Flush();
            fs.Seek(0, SeekOrigin.Begin);
            WriteWavHeader(fs, channels, sampleRate, bitsPerSample, isFloat, dataBytes);

            Console.WriteLine("captured " + dataBytes + " bytes of audio");
        }

        Marshal.FreeCoTaskMem(fmt);
    }

    static void WriteWavHeader(Stream s, short channels, int sampleRate,
                               short bits, bool isFloat, long dataBytes)
    {
        var w = new BinaryWriter(s);
        int blockAlign = channels * bits / 8;
        int byteRate = sampleRate * blockAlign;

        w.Write(new[] { 'R', 'I', 'F', 'F' });
        w.Write((int)(36 + dataBytes));
        w.Write(new[] { 'W', 'A', 'V', 'E' });
        w.Write(new[] { 'f', 'm', 't', ' ' });
        w.Write(16);
        w.Write((short)(isFloat ? 3 : 1));   // 3 = IEEE float, 1 = PCM
        w.Write(channels);
        w.Write(sampleRate);
        w.Write(byteRate);
        w.Write((short)blockAlign);
        w.Write(bits);
        w.Write(new[] { 'd', 'a', 't', 'a' });
        w.Write((int)dataBytes);
        w.Flush();
    }

    static void Check(int hr, string what)
    {
        if (hr != 0)
            throw new InvalidOperationException(what + " failed: 0x" + hr.ToString("X8"));
    }
}
