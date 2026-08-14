// Subscribes to SMTC events with NO polling at all. Any output proves the
// event path fires on its own, rather than the safety-net poll doing the work.
using System;
using System.Threading;
using Windows.Foundation;
using Windows.Media.Control;

static class EventProbe
{
    static T Await<T>(IAsyncOperation<T> op)
    {
        int waited = 0;
        while (op.Status == AsyncStatus.Started && waited < 10000) { Thread.Sleep(2); waited += 2; }
        if (op.Status != AsyncStatus.Completed) return default(T);
        return op.GetResults();
    }

    static void Stamp(string what)
    {
        Console.WriteLine("{0:HH:mm:ss.fff}  {1}", DateTime.Now, what);
    }

    static void Main(string[] args)
    {
        int seconds = args.Length > 0 ? int.Parse(args[0]) : 45;

        var mgr = Await(GlobalSystemMediaTransportControlsSessionManager.RequestAsync());
        if (mgr == null) { Console.WriteLine("no session manager"); return; }

        mgr.SessionsChanged += (a, b) => Stamp("SessionsChanged");

        foreach (var s in mgr.GetSessions())
        {
            string app = s.SourceAppUserModelId ?? "?";
            string shortName = app.Contains("AppleMusic") ? "AppleMusic"
                             : app.Contains("Spotify") ? "Spotify"
                             : app.Contains("hrome") ? "Chrome" : app;

            s.MediaPropertiesChanged += (a, b) =>
            {
                var p = Await(((GlobalSystemMediaTransportControlsSession)a).TryGetMediaPropertiesAsync());
                Stamp("MediaPropertiesChanged [" + shortName + "] -> " + (p == null ? "?" : p.Title));
            };
            s.PlaybackInfoChanged += (a, b) =>
            {
                var i = ((GlobalSystemMediaTransportControlsSession)a).GetPlaybackInfo();
                Stamp("PlaybackInfoChanged   [" + shortName + "] -> " + (i == null ? "?" : i.PlaybackStatus.ToString()));
            };
            Stamp("subscribed to " + shortName);
        }

        Stamp("listening (no polling)...");

        // Poke Apple Music with pause/play. Fully reversible, and unlike a skip
        // it doesn't cost a radio track. If events work, PlaybackInfoChanged
        // should land within milliseconds of each call.
        GlobalSystemMediaTransportControlsSession apple = null;
        foreach (var s in mgr.GetSessions())
            if ((s.SourceAppUserModelId ?? "").Contains("AppleMusic")) apple = s;

        if (apple != null)
        {
            Thread.Sleep(2000);
            Stamp(">>> calling TryPauseAsync");
            Await(apple.TryPauseAsync());
            Thread.Sleep(4000);
            Stamp(">>> calling TryPlayAsync");
            Await(apple.TryPlayAsync());
            Thread.Sleep(4000);
        }
        else Stamp("no Apple Music session to poke");

        Thread.Sleep(seconds * 1000);
        Stamp("done");
    }
}
