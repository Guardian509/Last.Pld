// Exercises the parts of Playlists.cs that don't need a real Spotify account:
// the DPAPI settings round trip, and the hand-rolled loopback HTTP catcher that
// receives the OAuth redirect. Build with Last.Pld.cs + Playlists.cs and
// /main:LastPld.TestHarness.
using System;
using System.Net;
using System.Threading;

namespace LastPld
{
    static class TestHarness
    {
        static int _fail;

        static void Check(string what, bool ok, string detail)
        {
            Console.WriteLine((ok ? "  PASS  " : "  FAIL  ") + what +
                              (detail == null ? "" : "   [" + detail + "]"));
            if (!ok) _fail++;
        }

        static void Main()
        {
            Console.WriteLine("=== settings ===");
            Settings.Set("test.plain", "hello");
            Check("plain value round trips", Settings.Get("test.plain") == "hello",
                  Settings.Get("test.plain"));

            Settings.SetSecret("test.secret", "super-secret-refresh-token");
            Check("secret round trips through DPAPI",
                  Settings.GetSecret("test.secret") == "super-secret-refresh-token",
                  Settings.GetSecret("test.secret"));
            Check("secret is not stored in the clear",
                  Settings.Get("test.secret").IndexOf("super-secret") < 0,
                  Settings.Get("test.secret"));

            Settings.Set("test.plain", "");
            Settings.Set("test.secret", "");

            Console.WriteLine();
            Console.WriteLine("=== oauth loopback catcher ===");
            RunCatcher("happy path", "?code=THE_CODE&state=ST123", "ST123", "THE_CODE", null);
            RunCatcher("state mismatch rejected", "?code=THE_CODE&state=WRONG", "ST123", null, "match");
            RunCatcher("user declined", "?error=access_denied&state=ST123", "ST123", null, "declined");
            RunCatcher("url-encoded code decoded", "?code=a%2Fb%2Bc&state=ST123", "ST123", "a/b+c", null);

            Console.WriteLine();
            Console.WriteLine("=== guard rails ===");
            string err = Playlists.Spotify.Add(new TrackIds { Title = "x", Isrc = "USWB11001072" });
            Check("Add without a connection fails cleanly",
                  err != null && err.IndexOf("connect", StringComparison.OrdinalIgnoreCase) >= 0, err);

            var ids = new TrackIds { Isrc = "us-wb1 1001072" };
            Check("ISRC normalised to bare uppercase",
                  ids.NormalisedIsrc == "USWB11001072", ids.NormalisedIsrc);

            Check("Apple Music reports the membership blocker",
                  Playlists.AppleMusic.Connect().IndexOf("Developer Program") >= 0, null);

            Console.WriteLine();
            Console.WriteLine("=== track-uri extraction (the album-uri trap) ===");
            // Spotify puts the album object -- with its own "uri" -- before the
            // track's uri in every item, so a naive first-"uri" read is wrong.
            string payload =
                "{\"tracks\":{\"items\":[{\"album\":{\"uri\":\"spotify:album:ALBUMID\"," +
                "\"name\":\"Hunting High and Low\"},\"artists\":[{\"uri\":\"spotify:artist:ARTISTID\"}]," +
                "\"name\":\"Take On Me\",\"uri\":\"spotify:track:2WfaOiMkCvy7F5fcp2zZ8L\"}]}}";
            string got = SpotifyTarget.FirstTrackUri(payload);
            Check("picks the track uri, not the album's",
                  got == "spotify:track:2WfaOiMkCvy7F5fcp2zZ8L", got);
            Check("no match returns null",
                  SpotifyTarget.FirstTrackUri("{\"tracks\":{\"items\":[]}}") == null, null);

            Console.WriteLine();
            Console.WriteLine("=== live reachability (real Spotify servers, no account) ===");
            LiveChecks();

            Console.WriteLine();
            Console.WriteLine(_fail == 0 ? "ALL PASSED" : _fail + " FAILED");
            Environment.Exit(_fail == 0 ? 0 : 1);
        }

        // Proves the HTTP/TLS plumbing and error parsing work against the real
        // API. Everything past the auth boundary needs a client ID we do not
        // have, so these stop exactly there -- deliberately.
        static void LiveChecks()
        {
            Http.Touch();
            try
            {
                HttpStatusCode st;
                string body = Http.Send("POST", "https://accounts.spotify.com/api/token",
                    "grant_type=refresh_token&refresh_token=nope&client_id=not-a-real-client",
                    "application/x-www-form-urlencoded", null, out st);

                Check("token endpoint reachable over TLS 1.2",
                      (int)st >= 400 && (int)st < 500, "HTTP " + (int)st);
                Check("error body parses into a readable reason",
                      Json.Str(body, "error") != null || Json.Str(body, "error_description") != null,
                      (Json.Str(body, "error") ?? "") + " / " + (Json.Str(body, "error_description") ?? ""));

                body = Http.Send("GET",
                    "https://api.spotify.com/v1/search?type=track&limit=1&market=US&q=isrc%3AUSWB11001072",
                    null, null, "definitely-not-a-token", out st);

                Check("search endpoint reachable, rejects a bad token",
                      st == HttpStatusCode.Unauthorized, "HTTP " + (int)st);
                Check("401 message surfaces to the user",
                      !string.IsNullOrEmpty(Json.Str(body, "message")),
                      Json.Str(body, "message"));
            }
            catch (Exception ex)
            {
                Check("live reachability", false, ex.GetType().Name + ": " + ex.Message);
            }
        }

        static void RunCatcher(string name, string query, string expectState,
                               string expectCode, string expectErrorContains)
        {
            int port = 8899;
            using (var catcher = new LoopbackCatcher(port))
            {
                if (catcher.Error != null) { Check(name, false, catcher.Error); return; }

                var hit = new Thread(() =>
                {
                    Thread.Sleep(300);
                    try
                    {
                        var req = (HttpWebRequest)WebRequest.Create(
                            "http://127.0.0.1:" + port + "/callback" + query);
                        req.Timeout = 10000;
                        using (var r = req.GetResponse()) { }
                    }
                    catch { }
                });
                hit.IsBackground = true;
                hit.Start();

                string code, error;
                bool ok = catcher.Wait(15000, expectState, out code, out error);

                if (expectCode != null)
                    Check(name, ok && code == expectCode, "code=" + code + " err=" + error);
                else
                    Check(name,
                          !ok && error != null &&
                          error.IndexOf(expectErrorContains, StringComparison.OrdinalIgnoreCase) >= 0,
                          "err=" + error);
            }
        }
    }
}
