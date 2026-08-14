// Dumps every SMTC media property for every session, to find a field that
// reliably separates real music (YouTube, Apple Music) from page-title noise
// (Facebook, Airbnb) inside a browser.
using System;
using System.Threading;
using Windows.Foundation;
using Windows.Media.Control;

static class DumpProps
{
    static T Await<T>(IAsyncOperation<T> op)
    {
        int waited = 0;
        while (op.Status == AsyncStatus.Started && waited < 10000) { Thread.Sleep(2); waited += 2; }
        if (op.Status != AsyncStatus.Completed) return default(T);
        return op.GetResults();
    }

    static string S(string v) { return v == null ? "<null>" : (v.Length == 0 ? "<empty>" : v); }

    static void Main()
    {
        var mgr = Await(GlobalSystemMediaTransportControlsSessionManager.RequestAsync());
        if (mgr == null) { Console.WriteLine("no manager"); return; }

        foreach (var s in mgr.GetSessions())
        {
            Console.WriteLine("================================================");
            Console.WriteLine("AUMID        : " + S(s.SourceAppUserModelId));

            try
            {
                var i = s.GetPlaybackInfo();
                Console.WriteLine("Status       : " + (i == null ? "?" : i.PlaybackStatus.ToString()));
                if (i != null)
                    Console.WriteLine("PlaybackType : " +
                        (i.PlaybackType.HasValue ? i.PlaybackType.Value.ToString() : "<null>"));
            }
            catch (Exception ex) { Console.WriteLine("playbackinfo err: " + ex.Message); }

            var p = Await(s.TryGetMediaPropertiesAsync());
            if (p == null) { Console.WriteLine("  (no media properties)"); continue; }

            Console.WriteLine("Title        : " + S(p.Title));
            Console.WriteLine("Artist       : " + S(p.Artist));
            Console.WriteLine("AlbumTitle   : " + S(p.AlbumTitle));
            Console.WriteLine("AlbumArtist  : " + S(p.AlbumArtist));
            Console.WriteLine("Subtitle     : " + S(p.Subtitle));
            Console.WriteLine("TrackNumber  : " + p.TrackNumber);
            Console.WriteLine("AlbumTrackCt : " + p.AlbumTrackCount);
            Console.WriteLine("Thumbnail    : " + (p.Thumbnail == null ? "<null>" : "present"));

            try
            {
                var g = p.Genres;
                Console.WriteLine("Genres       : " + (g == null || g.Count == 0 ? "<none>" : string.Join("; ", g)));
            }
            catch { Console.WriteLine("Genres       : <err>"); }

            try
            {
                Console.WriteLine("PropsPbType  : " +
                    (p.PlaybackType.HasValue ? p.PlaybackType.Value.ToString() : "<null>"));
            }
            catch { Console.WriteLine("PropsPbType  : <err>"); }
        }
    }
}
