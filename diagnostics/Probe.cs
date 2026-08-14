// Times each stage of Last.Pld's polling cycle, to locate detection latency.
using System;
using System.Diagnostics;
using System.Threading;
using Windows.Foundation;
using Windows.Media.Control;

static class Probe
{
    static T Await<T>(IAsyncOperation<T> op)
    {
        int waited = 0;
        while (op.Status == AsyncStatus.Started && waited < 10000) { Thread.Sleep(20); waited += 20; }
        if (op.Status != AsyncStatus.Completed) return default(T);
        return op.GetResults();
    }

    static void Main()
    {
        var total = Stopwatch.StartNew();

        for (int round = 1; round <= 3; round++)
        {
            Console.WriteLine("--- round " + round + " ---");
            var sw = Stopwatch.StartNew();
            var mgr = Await(GlobalSystemMediaTransportControlsSessionManager.RequestAsync());
            Console.WriteLine("  RequestAsync            {0,6} ms", sw.ElapsedMilliseconds);

            sw.Restart();
            var sessions = mgr.GetSessions();
            Console.WriteLine("  GetSessions             {0,6} ms  ({1} sessions)",
                sw.ElapsedMilliseconds, sessions.Count);

            foreach (var s in sessions)
            {
                string app = s.SourceAppUserModelId ?? "?";
                sw.Restart();
                var info = s.GetPlaybackInfo();
                long tInfo = sw.ElapsedMilliseconds;

                sw.Restart();
                var props = Await(s.TryGetMediaPropertiesAsync());
                long tProps = sw.ElapsedMilliseconds;

                Console.WriteLine("    {0,-45} info {1,4}ms  props {2,4}ms  status={3}",
                    app.Length > 45 ? app.Substring(0, 45) : app,
                    tInfo, tProps,
                    info == null ? "?" : info.PlaybackStatus.ToString());
            }
            Console.WriteLine("  cycle total so far      {0,6} ms", total.ElapsedMilliseconds);
            total.Restart();
        }
    }
}
