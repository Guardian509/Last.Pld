// Measures end-to-end detection latency: force a track change via SMTC, then
// time how long until Last.Pld writes the new row to its CSV.
using System;
using System.IO;
using System.Text;
using System.Threading;
using Windows.Foundation;
using Windows.Media.Control;

static class MeasureLag
{
    static T Await<T>(IAsyncOperation<T> op)
    {
        int waited = 0;
        while (op.Status == AsyncStatus.Started && waited < 10000) { Thread.Sleep(2); waited += 2; }
        if (op.Status != AsyncStatus.Completed) return default(T);
        return op.GetResults();
    }

    static int Lines(string path)
    {
        try
        {
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
            {
                int n = 0;
                while (sr.ReadLine() != null) n++;
                return n;
            }
        }
        catch { return -1; }
    }

    static void Main(string[] args)
    {
        string csv = args.Length > 0 ? args[0] : @"C:\Users\Nicka\NowPlaying\lastpld.csv";

        var mgr = Await(GlobalSystemMediaTransportControlsSessionManager.RequestAsync());
        GlobalSystemMediaTransportControlsSession apple = null;
        foreach (var s in mgr.GetSessions())
            if ((s.SourceAppUserModelId ?? "").Contains("AppleMusic")) apple = s;

        if (apple == null) { Console.WriteLine("no Apple Music session"); return; }

        int before = Lines(csv);
        Console.WriteLine("rows before      : " + before);

        var sw = System.Diagnostics.Stopwatch.StartNew();
        Console.WriteLine("skipping track at: " + DateTime.Now.ToString("HH:mm:ss.fff"));
        Await(apple.TrySkipNextAsync());

        while (sw.ElapsedMilliseconds < 30000)
        {
            int now = Lines(csv);
            if (now > before)
            {
                Console.WriteLine("row appeared at  : " + DateTime.Now.ToString("HH:mm:ss.fff"));
                Console.WriteLine(">>> DETECTION LATENCY: " + sw.ElapsedMilliseconds + " ms");
                return;
            }
            Thread.Sleep(25);
        }
        Console.WriteLine(">>> no new row within 30s");
    }
}
