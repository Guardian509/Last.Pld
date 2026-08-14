// Playlist targets for Last.Pld.
//
// When Identify recognises a song, Shazam's response carries enough to place it
// exactly, with no title-matching guesswork:
//
//   * Apple Music  - an exact catalog id, in hub.actions[<applemusicplay>].id
//   * ISRC         - track.isrc, the industry recording id
//
// Spotify is the one that needs a step: Shazam's SPOTIFY "provider" entry is
// only a *search deeplink* (spotify:search:Take%20On%20Me%20a-ha), not a track
// URI. So we resolve the ISRC through Spotify's own search, which is an exact
// lookup rather than a fuzzy one, and only fall back to title/artist when a
// track has no ISRC.
//
// There is deliberately no "log in with Shazam" here, because no such thing
// exists: Shazam has no public account API, and shazamio talks to Shazam's
// internal endpoint anonymously. The login belongs to the destination service.
//
// Spotify is implemented. Apple Music is stubbed behind the same interface --
// its blocker is commercial, not technical (see AppleMusicTarget).

using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;

namespace LastPld
{
    // ------------------------------------------------------------ track ids

    // What a recognition gives us to hand to a streaming service.
    class TrackIds
    {
        public string Title = "";
        public string Artist = "";
        public string Album = "";
        public string Isrc = "";            // USWB11001072 -- exact recording id
        public string SpotifyUri = "";      // usually empty; Shazam gives a search link
        public string AppleMusicUrl = "";   // https://music.apple.com/...?i=380907765
        public string AppleTrackId = "";    // 380907765 -- exact Apple Music catalog id

        // Enough to look the track up somewhere. Title+artist alone counts,
        // since Spotify search can still find it, just less certainly.
        public bool HasAnything
        {
            get
            {
                return Isrc.Length > 0 || SpotifyUri.Length > 0 ||
                       AppleTrackId.Length > 0 || Title.Length > 0;
            }
        }

        // ISRCs must reach Spotify as 12 uppercase alphanumerics; its search
        // silently returns nothing for lowercase or dashed forms.
        public string NormalisedIsrc
        {
            get
            {
                if (Isrc == null) return "";
                var sb = new StringBuilder();
                foreach (char c in Isrc)
                    if (char.IsLetterOrDigit(c)) sb.Append(char.ToUpperInvariant(c));
                return sb.ToString();
            }
        }
    }

    // --------------------------------------------------------------- target

    interface IPlaylistTarget
    {
        string Name { get; }
        bool IsConfigured { get; }   // do we have what we need to even try?
        bool IsConnected { get; }    // is there a stored credential?
        string Connect();            // null on success, otherwise the reason
        void Disconnect();
        string Add(TrackIds track);  // null on success, otherwise the reason
    }

    // ------------------------------------------------------------- settings

    // Plain key=value next to the exe. Secrets are DPAPI-encrypted to the
    // current user, so a stolen copy of the file is useless on another machine.
    static class Settings
    {
        static readonly object Gate = new object();
        static Dictionary<string, string> _values;
        static string _path;

        static string Path_
        {
            get
            {
                if (_path == null)
                    _path = System.IO.Path.Combine(
                        System.IO.Path.GetDirectoryName(
                            System.Windows.Forms.Application.ExecutablePath),
                        "playlists.txt");
                return _path;
            }
        }

        static void Load()
        {
            if (_values != null) return;
            _values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            try
            {
                if (!File.Exists(Path_)) return;
                foreach (var raw in File.ReadAllLines(Path_, Encoding.UTF8))
                {
                    string line = raw.Trim();
                    if (line.Length == 0 || line.StartsWith("#")) continue;
                    int eq = line.IndexOf('=');
                    if (eq <= 0) continue;
                    _values[line.Substring(0, eq).Trim()] = line.Substring(eq + 1).Trim();
                }
            }
            catch { }
        }

        static void Save()
        {
            try
            {
                var sb = new StringBuilder();
                sb.Append("# Last.Pld playlist settings. Secrets here are encrypted\r\n");
                sb.Append("# to this Windows user; copying them elsewhere gains nothing.\r\n");
                foreach (var kv in _values)
                    sb.Append(kv.Key).Append("=").Append(kv.Value).Append("\r\n");
                File.WriteAllText(Path_, sb.ToString(), new UTF8Encoding(true));
            }
            catch { }
        }

        public static string Get(string key, string fallback = "")
        {
            lock (Gate)
            {
                Load();
                string v;
                return _values.TryGetValue(key, out v) ? v : fallback;
            }
        }

        public static void Set(string key, string value)
        {
            lock (Gate)
            {
                Load();
                if (string.IsNullOrEmpty(value)) _values.Remove(key);
                else _values[key] = value;
                Save();
            }
        }

        public static string GetSecret(string key)
        {
            string blob = Get(key);
            if (blob.Length == 0) return "";
            try
            {
                byte[] raw = ProtectedData.Unprotect(
                    Convert.FromBase64String(blob), null, DataProtectionScope.CurrentUser);
                return Encoding.UTF8.GetString(raw);
            }
            catch { return ""; }   // machine changed, or file hand-edited
        }

        public static void SetSecret(string key, string value)
        {
            if (string.IsNullOrEmpty(value)) { Set(key, ""); return; }
            try
            {
                byte[] enc = ProtectedData.Protect(
                    Encoding.UTF8.GetBytes(value), null, DataProtectionScope.CurrentUser);
                Set(key, Convert.ToBase64String(enc));
            }
            catch { }
        }
    }

    // ------------------------------------------------------------------ json

    // Just enough JSON to read flat string and number fields out of the
    // responses we ask for. Deliberately not a parser -- the app ships as a
    // single dependency-free exe and every response we touch is shallow.
    static class Json
    {
        public static string Str(string json, string key)
        {
            int i = Seek(json, key);
            if (i < 0) return null;
            while (i < json.Length && json[i] == ' ') i++;
            if (i >= json.Length || json[i] != '"') return null;
            i++;

            var sb = new StringBuilder();
            while (i < json.Length && json[i] != '"')
            {
                if (json[i] == '\\' && i + 1 < json.Length)
                {
                    i++;
                    char c = json[i];
                    if (c == 'n') sb.Append('\n');
                    else if (c == 't') sb.Append('\t');
                    else if (c == 'r') sb.Append('\r');
                    else if (c == 'u' && i + 4 < json.Length)
                    {
                        sb.Append((char)Convert.ToInt32(json.Substring(i + 1, 4), 16));
                        i += 4;
                    }
                    else sb.Append(c);
                }
                else sb.Append(json[i]);
                i++;
            }
            return sb.ToString();
        }

        public static int Num(string json, string key, int fallback)
        {
            int i = Seek(json, key);
            if (i < 0) return fallback;
            while (i < json.Length && json[i] == ' ') i++;
            int start = i;
            while (i < json.Length && (char.IsDigit(json[i]) || json[i] == '-')) i++;
            int n;
            return (i > start && int.TryParse(json.Substring(start, i - start), out n))
                ? n : fallback;
        }

        static int Seek(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return -1;
            string needle = "\"" + key + "\":";
            int i = json.IndexOf(needle, StringComparison.Ordinal);
            return i < 0 ? -1 : i + needle.Length;
        }

        public static string Escape(string s)
        {
            if (s == null) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"")
                    .Replace("\r", "\\r").Replace("\n", "\\n");
        }
    }

    // ------------------------------------------------------------------ http

    static class Http
    {
        static Http()
        {
            // .NET Framework 4.x still negotiates TLS 1.0 by default on some
            // boxes; every one of these APIs is TLS 1.2+ only, and the failure
            // it produces otherwise ("connection was closed unexpectedly") is
            // deeply unhelpful.
            try
            {
                ServicePointManager.SecurityProtocol =
                    SecurityProtocolType.Tls12 | (SecurityProtocolType)3072;
            }
            catch { }
        }

        public static void Touch() { }   // forces the static ctor to run

        // Returns the body. On an HTTP error, returns the error body and sets
        // status, because these APIs explain themselves in the error payload.
        public static string Send(string method, string url, string body,
                                  string contentType, string bearer,
                                  out HttpStatusCode status)
        {
            status = 0;
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = method;
            req.Timeout = 30000;
            req.UserAgent = "Last.Pld";
            if (bearer != null) req.Headers["Authorization"] = "Bearer " + bearer;

            if (body != null)
            {
                byte[] data = Encoding.UTF8.GetBytes(body);
                req.ContentType = contentType;
                req.ContentLength = data.Length;
                using (var s = req.GetRequestStream()) s.Write(data, 0, data.Length);
            }

            try
            {
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    status = resp.StatusCode;
                    using (var r = new StreamReader(resp.GetResponseStream()))
                        return r.ReadToEnd();
                }
            }
            catch (WebException ex)
            {
                var resp = ex.Response as HttpWebResponse;
                if (resp == null) throw;
                status = resp.StatusCode;
                using (var r = new StreamReader(resp.GetResponseStream()))
                    return r.ReadToEnd();
            }
        }
    }

    // --------------------------------------------------------------- spotify

    class SpotifyTarget : IPlaylistTarget
    {
        // playlist-read-private is not requested: we never browse the user's
        // playlists, we create our own once and remember its id.
        const string Scopes = "playlist-modify-private playlist-modify-public";
        const string AuthUrl = "https://accounts.spotify.com/authorize";
        const string TokenUrl = "https://accounts.spotify.com/api/token";
        const string ApiBase = "https://api.spotify.com/v1";
        const string PlaylistName = "Last.Pld";

        string _access = "";
        DateTime _accessExpires = DateTime.MinValue;

        public string Name { get { return "Spotify"; } }

        public bool IsConfigured { get { return ClientId.Length > 0; } }
        public bool IsConnected { get { return Settings.GetSecret("spotify.refresh_token").Length > 0; } }

        static string ClientId { get { return Settings.Get("spotify.client_id"); } }
        static int Port
        {
            get
            {
                int p;
                return int.TryParse(Settings.Get("spotify.redirect_port", "8888"), out p) ? p : 8888;
            }
        }
        static string RedirectUri
        {
            // Must be the loopback literal, not "localhost": Spotify stopped
            // accepting localhost for clients created after 2025-04-09, and
            // plain http is only permitted for loopback IPs.
            get { return "http://127.0.0.1:" + Port + "/callback"; }
        }

        public static string RedirectUriForSetup { get { return RedirectUri; } }

        // ---- connect (Authorization Code + PKCE; no client secret to leak) --

        public string Connect()
        {
            Http.Touch();
            if (!IsConfigured)
                return "No Spotify client ID set yet.";

            string verifier = RandomUrlSafe(64);
            string challenge = Base64Url(Sha256(Encoding.ASCII.GetBytes(verifier)));
            string state = RandomUrlSafe(16);

            string url = AuthUrl
                + "?client_id=" + Uri.EscapeDataString(ClientId)
                + "&response_type=code"
                + "&redirect_uri=" + Uri.EscapeDataString(RedirectUri)
                + "&code_challenge_method=S256"
                + "&code_challenge=" + challenge
                + "&state=" + state
                + "&scope=" + Uri.EscapeDataString(Scopes);

            string code, error;
            using (var listener = new LoopbackCatcher(Port))
            {
                if (listener.Error != null) return listener.Error;
                try { System.Diagnostics.Process.Start(url); }
                catch (Exception ex) { return "Couldn't open the browser: " + ex.Message; }

                if (!listener.Wait(180000, state, out code, out error))
                    return error ?? "Timed out waiting for the Spotify sign-in.";
            }

            string form = "grant_type=authorization_code"
                + "&code=" + Uri.EscapeDataString(code)
                + "&redirect_uri=" + Uri.EscapeDataString(RedirectUri)
                + "&client_id=" + Uri.EscapeDataString(ClientId)
                + "&code_verifier=" + Uri.EscapeDataString(verifier);

            HttpStatusCode st;
            string body = Http.Send("POST", TokenUrl, form,
                                    "application/x-www-form-urlencoded", null, out st);
            if (st != HttpStatusCode.OK)
                return "Spotify refused the sign-in: " + Describe(body);

            string refresh = Json.Str(body, "refresh_token");
            _access = Json.Str(body, "access_token") ?? "";
            _accessExpires = DateTime.Now.AddSeconds(Json.Num(body, "expires_in", 3600) - 60);
            if (string.IsNullOrEmpty(refresh) || _access.Length == 0)
                return "Spotify returned no usable token.";

            Settings.SetSecret("spotify.refresh_token", refresh);

            string err = EnsurePlaylist();
            return err;
        }

        public void Disconnect()
        {
            Settings.SetSecret("spotify.refresh_token", "");
            Settings.Set("spotify.playlist_id", "");
            _access = "";
            _accessExpires = DateTime.MinValue;
        }

        // ---- tokens --------------------------------------------------------

        string EnsureAccess()
        {
            if (_access.Length > 0 && DateTime.Now < _accessExpires) return null;

            string refresh = Settings.GetSecret("spotify.refresh_token");
            if (refresh.Length == 0) return "Spotify isn't connected.";

            string form = "grant_type=refresh_token"
                + "&refresh_token=" + Uri.EscapeDataString(refresh)
                + "&client_id=" + Uri.EscapeDataString(ClientId);

            HttpStatusCode st;
            string body = Http.Send("POST", TokenUrl, form,
                                    "application/x-www-form-urlencoded", null, out st);
            if (st != HttpStatusCode.OK)
                return "Spotify sign-in expired, reconnect: " + Describe(body);

            _access = Json.Str(body, "access_token") ?? "";
            _accessExpires = DateTime.Now.AddSeconds(Json.Num(body, "expires_in", 3600) - 60);

            // Spotify rotates refresh tokens; keep the new one when offered.
            string rotated = Json.Str(body, "refresh_token");
            if (!string.IsNullOrEmpty(rotated)) Settings.SetSecret("spotify.refresh_token", rotated);

            return _access.Length > 0 ? null : "Spotify returned no access token.";
        }

        // ---- playlist ------------------------------------------------------

        string EnsurePlaylist()
        {
            if (Settings.Get("spotify.playlist_id").Length > 0) return null;

            string err = EnsureAccess();
            if (err != null) return err;

            // POST /me/playlists -- the old /users/{id}/playlists form was
            // removed in Spotify's February 2026 API change.
            string body = "{\"name\":\"" + PlaylistName + "\","
                        + "\"public\":false,"
                        + "\"description\":\"Songs identified by Last.Pld.\"}";

            HttpStatusCode st;
            string resp = Http.Send("POST", ApiBase + "/me/playlists", body,
                                    "application/json", _access, out st);
            if (st != HttpStatusCode.Created && st != HttpStatusCode.OK)
                return "Couldn't create the playlist: " + Describe(resp);

            string id = Json.Str(resp, "id");
            if (string.IsNullOrEmpty(id)) return "Spotify created no playlist id.";
            Settings.Set("spotify.playlist_id", id);
            return null;
        }

        // ---- add -----------------------------------------------------------

        public string Add(TrackIds track)
        {
            Http.Touch();
            if (track == null) return "Nothing to add.";
            if (!IsConnected) return "Spotify isn't connected.";

            string err = EnsureAccess();
            if (err != null) return err;
            err = EnsurePlaylist();
            if (err != null) return err;

            string uri = Resolve(track, out err);
            if (uri == null) return err;

            string playlist = Settings.Get("spotify.playlist_id");
            string body = "{\"uris\":[\"" + Json.Escape(uri) + "\"]}";

            // POST /playlists/{id}/items -- /tracks was removed in Feb 2026.
            HttpStatusCode st;
            string resp = Http.Send("POST", ApiBase + "/playlists/" + playlist + "/items",
                                    body, "application/json", _access, out st);

            if (st == HttpStatusCode.Created || st == HttpStatusCode.OK) return null;

            // The stored playlist can go stale if the user deletes it; drop it
            // so the next attempt makes a fresh one rather than failing forever.
            if (st == HttpStatusCode.NotFound) Settings.Set("spotify.playlist_id", "");

            return "Spotify rejected the add: " + Describe(resp);
        }

        // ---- resolving a Shazam hit to a Spotify track ----------------------

        // ISRC first: that's an exact recording match. Title/artist is only a
        // fallback for tracks Shazam has no ISRC for.
        string Resolve(TrackIds track, out string error)
        {
            error = null;

            if (track.SpotifyUri.StartsWith("spotify:track:", StringComparison.Ordinal))
                return track.SpotifyUri;

            string market = Settings.Get("spotify.market", "US");

            string isrc = track.NormalisedIsrc;
            if (isrc.Length > 0)
            {
                string uri = Search("isrc:" + isrc, market, out error);
                if (uri != null) return uri;
                if (error != null) return null;
            }

            if (track.Title.Length > 0)
            {
                string q = track.Title;
                if (track.Artist.Length > 0) q += " artist:" + track.Artist;
                string uri = Search(q, market, out error);
                if (uri != null) return uri;
                if (error != null) return null;
            }

            error = "Spotify doesn't seem to have this track.";
            return null;
        }

        // Returns a track URI, or null with error set only on a real failure --
        // "found nothing" is null with error left null.
        string Search(string query, string market, out string error)
        {
            error = null;
            string url = ApiBase + "/search?type=track&limit=1"
                       + "&market=" + Uri.EscapeDataString(market)
                       + "&q=" + Uri.EscapeDataString(query);

            HttpStatusCode st;
            string body = Http.Send("GET", url, null, null, _access, out st);
            if (st != HttpStatusCode.OK)
            {
                error = "Spotify search failed: " + Describe(body);
                return null;
            }
            return FirstTrackUri(body);
        }

        // Pull the first spotify:track: URI out of the payload. Reading the
        // first "uri" field instead would grab the *album's* URI, which appears
        // earlier in every track object.
        internal static string FirstTrackUri(string body)   // internal: exercised by the test harness
        {
            if (string.IsNullOrEmpty(body)) return null;
            const string marker = "spotify:track:";
            int i = body.IndexOf(marker, StringComparison.Ordinal);
            if (i < 0) return null;
            int end = i;
            while (end < body.Length && body[end] != '"') end++;
            string uri = body.Substring(i, end - i);
            return uri.Length > marker.Length ? uri : null;
        }

        static string Describe(string body)
        {
            string m = Json.Str(body, "message");
            if (!string.IsNullOrEmpty(m)) return m;
            m = Json.Str(body, "error_description");
            if (!string.IsNullOrEmpty(m)) return m;
            m = Json.Str(body, "error");
            if (!string.IsNullOrEmpty(m)) return m;
            if (string.IsNullOrEmpty(body)) return "no detail given";
            return body.Length > 200 ? body.Substring(0, 200) : body;
        }

        // ---- pkce helpers --------------------------------------------------

        static byte[] Sha256(byte[] data)
        {
            using (var sha = new SHA256Managed()) return sha.ComputeHash(data);
        }

        static string Base64Url(byte[] data)
        {
            return Convert.ToBase64String(data)
                .Replace('+', '-').Replace('/', '_').TrimEnd('=');
        }

        static string RandomUrlSafe(int length)
        {
            const string alphabet =
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
            var bytes = new byte[length];
            using (var rng = new RNGCryptoServiceProvider()) rng.GetBytes(bytes);
            var sb = new StringBuilder(length);
            foreach (var b in bytes) sb.Append(alphabet[b % alphabet.Length]);
            return sb.ToString();
        }
    }

    // ------------------------------------------------- oauth loopback catcher

    // A bare TcpListener rather than HttpListener: HttpListener needs a URL ACL
    // reservation (netsh http add urlacl) or elevation for anything but a
    // handful of prefixes, which is a miserable thing to inflict on someone
    // just trying to connect their account. We only need to read one GET line.
    class LoopbackCatcher : IDisposable
    {
        readonly TcpListener _listener;
        public string Error;

        public LoopbackCatcher(int port)
        {
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, port);
                _listener.Start();
            }
            catch (SocketException ex)
            {
                Error = "Couldn't listen on 127.0.0.1:" + port +
                        " for the sign-in redirect (" + ex.SocketErrorCode + "). " +
                        "Something else may be using that port.";
            }
        }

        public bool Wait(int timeoutMs, string expectedState, out string code, out string error)
        {
            code = null;
            error = null;
            if (_listener == null) { error = Error; return false; }

            var deadline = DateTime.Now.AddMilliseconds(timeoutMs);
            while (DateTime.Now < deadline)
            {
                if (!_listener.Pending()) { System.Threading.Thread.Sleep(100); continue; }

                using (var client = _listener.AcceptTcpClient())
                using (var stream = client.GetStream())
                {
                    string request = ReadRequestLine(stream);
                    string query = QueryOf(request);

                    string got = Param(query, "state");
                    string err = Param(query, "error");
                    code = Param(query, "code");

                    string page;
                    if (err != null)
                    {
                        error = "Spotify sign-in was declined (" + err + ").";
                        page = "Sign-in declined. You can close this tab.";
                        code = null;
                    }
                    else if (expectedState != null && got != expectedState)
                    {
                        // Wrong state means this redirect isn't the one we started.
                        error = "The sign-in response didn't match this request.";
                        page = "Mismatched sign-in. You can close this tab.";
                        code = null;
                    }
                    else if (string.IsNullOrEmpty(code))
                    {
                        error = "Spotify returned no authorisation code.";
                        page = "No code returned. You can close this tab.";
                    }
                    else
                    {
                        page = "Last.Pld is connected to Spotify. You can close this tab.";
                    }

                    Respond(stream, page);
                    return code != null;
                }
            }
            error = "Timed out waiting for the Spotify sign-in.";
            return false;
        }

        static string ReadRequestLine(NetworkStream stream)
        {
            var sb = new StringBuilder();
            var buf = new byte[1];
            int guard = 0;
            stream.ReadTimeout = 15000;
            while (guard++ < 8192)
            {
                int n;
                try { n = stream.Read(buf, 0, 1); } catch { break; }
                if (n <= 0) break;
                if (buf[0] == '\n') break;
                if (buf[0] != '\r') sb.Append((char)buf[0]);
            }
            return sb.ToString();
        }

        static string QueryOf(string requestLine)
        {
            int q = requestLine.IndexOf('?');
            if (q < 0) return "";
            int sp = requestLine.IndexOf(' ', q);
            return sp < 0 ? requestLine.Substring(q + 1) : requestLine.Substring(q + 1, sp - q - 1);
        }

        static string Param(string query, string key)
        {
            foreach (var pair in query.Split('&'))
            {
                int eq = pair.IndexOf('=');
                if (eq <= 0) continue;
                if (pair.Substring(0, eq) == key)
                    return Uri.UnescapeDataString(pair.Substring(eq + 1));
            }
            return null;
        }

        static void Respond(NetworkStream stream, string message)
        {
            string html =
                "<!doctype html><meta charset=utf-8><title>Last.Pld</title>" +
                "<body style=\"font-family:Segoe UI,sans-serif;background:#202020;" +
                "color:#e0e0e0;display:flex;align-items:center;justify-content:center;" +
                "height:100vh;margin:0\"><p>" + message + "</p></body>";
            byte[] payload = Encoding.UTF8.GetBytes(html);
            byte[] head = Encoding.ASCII.GetBytes(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n" +
                "Content-Length: " + payload.Length + "\r\nConnection: close\r\n\r\n");
            try
            {
                stream.Write(head, 0, head.Length);
                stream.Write(payload, 0, payload.Length);
                stream.Flush();
            }
            catch { }
        }

        public void Dispose()
        {
            try { if (_listener != null) _listener.Stop(); } catch { }
        }
    }

    // ----------------------------------------------------------- apple music

    // Not implemented, and the blocker is commercial rather than technical.
    //
    // Every Apple Music API call needs a *developer token*: a JWT signed with a
    // MusicKit private key. Creating that key requires a paid Apple Developer
    // Program membership (99 USD/year) -- a free Apple ID cannot make one, and
    // there is no way around it. On top of that the user needs a *Music User
    // Token*, obtained by signing in through MusicKit JS in a browser or
    // webview, since there is no native MusicKit for Windows.
    //
    // If that membership ever exists, the rest is short, and the id we need is
    // already in hand as TrackIds.AppleTrackId:
    //
    //   POST https://api.music.apple.com/v1/me/library/playlists/{id}/tracks
    //   Authorization: Bearer <developer token>
    //   Music-User-Token: <music user token>
    //   {"data":[{"id":"<AppleTrackId>","type":"songs"}]}
    //
    // Worth knowing: the Shazam *app* already syncs its own recognitions into a
    // "My Shazam Tracks" playlist. That covers phone Shazams only -- Last.Pld's
    // desktop recognitions never touch the account, so it cannot substitute.
    class AppleMusicTarget : IPlaylistTarget
    {
        public string Name { get { return "Apple Music"; } }
        public bool IsConfigured { get { return false; } }
        public bool IsConnected { get { return false; } }

        const string Why =
            "Apple Music needs a paid Apple Developer Program membership " +
            "(99 USD/year) to sign the developer token every request requires. " +
            "A free Apple ID cannot create the MusicKit key, so there is no " +
            "way to enable this without that membership.";

        public string Connect() { return Why; }
        public void Disconnect() { }
        public string Add(TrackIds track) { return Why; }
    }

    // ------------------------------------------------------- spotify setup UI

    // Connecting needs a client ID, and only the account owner can create one,
    // so the app has to ask. This walks through it rather than just failing
    // with "no client ID set".
    class SpotifySetupForm : Form
    {
        readonly TextBox _clientId = new TextBox();
        readonly Button _connect = new Button();
        readonly Label _status = new Label();

        public SpotifySetupForm()
        {
            Text = "Connect Spotify";
            Width = 660;
            Height = 460;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(32, 32, 32);
            ForeColor = Color.Gainsboro;
            Font = new Font("Segoe UI", 9f);
            Icon = AppIcon.Get();

            var heading = new Label
            {
                Text = "Send identified songs to a Spotify playlist",
                Left = 18, Top = 16, Width = 600, Height = 28,
                Font = new Font("Segoe UI", 12f, FontStyle.Bold),
                ForeColor = Color.FromArgb(250, 90, 100),
                UseMnemonic = false
            };

            var blurb = new Label
            {
                Left = 20, Top = 52, Width = 610, Height = 108,
                UseMnemonic = false,
                ForeColor = Color.Gainsboro,
                Text =
                    "Spotify only lets an account's own app write to its playlists, so this " +
                    "needs a free developer app of yours. It takes about a minute and costs " +
                    "nothing.\r\n\r\n" +
                    "    1.  Open the Spotify developer dashboard and click Create app.\r\n" +
                    "    2.  Give it any name - \"Last.Pld\" is fine.\r\n" +
                    "    3.  Add the Redirect URI below, exactly as shown.\r\n" +
                    "    4.  Tick Web API, save, then copy the app's Client ID here."
            };

            var open = MakeButton("Open dashboard", 20, 168, 130);
            open.Click += (s, e) =>
            {
                try { System.Diagnostics.Process.Start("https://developer.spotify.com/dashboard"); }
                catch { }
            };

            var uriLabel = new Label
            {
                Text = "Redirect URI", Left = 20, Top = 214, Width = 90,
                ForeColor = Color.Gray, UseMnemonic = false
            };
            var uriBox = new TextBox
            {
                Left = 114, Top = 210, Width = 400,
                ReadOnly = true,
                Text = SpotifyTarget.RedirectUriForSetup,
                BackColor = Color.FromArgb(50, 50, 50),
                ForeColor = Color.White,
                BorderStyle = BorderStyle.FixedSingle
            };
            var copy = MakeButton("Copy", 522, 209, 100);
            copy.Click += (s, e) => { try { Clipboard.SetText(uriBox.Text); } catch { } };

            var warn = new Label
            {
                Left = 114, Top = 238, Width = 510, Height = 34,
                ForeColor = Color.FromArgb(230, 170, 90),
                UseMnemonic = false,
                Text = "It must be 127.0.0.1, not localhost - Spotify stopped accepting " +
                       "localhost, and plain http is only allowed for loopback addresses."
            };

            var idLabel = new Label
            {
                Text = "Client ID", Left = 20, Top = 292, Width = 90,
                ForeColor = Color.Gray, UseMnemonic = false
            };
            _clientId.Left = 114; _clientId.Top = 288; _clientId.Width = 400;
            _clientId.BackColor = Color.FromArgb(50, 50, 50);
            _clientId.ForeColor = Color.White;
            _clientId.BorderStyle = BorderStyle.FixedSingle;
            _clientId.Text = Settings.Get("spotify.client_id");

            _connect.Text = "Connect";
            _connect.Left = 522; _connect.Top = 287; _connect.Width = 100; _connect.Height = 26;
            _connect.FlatStyle = FlatStyle.Flat;
            _connect.BackColor = Color.FromArgb(120, 40, 48);
            _connect.ForeColor = Color.Gainsboro;
            _connect.FlatAppearance.BorderColor = Color.FromArgb(80, 80, 80);
            _connect.Click += (s, e) => Go();

            _status.Left = 20; _status.Top = 330; _status.Width = 604; _status.Height = 60;
            _status.ForeColor = Color.Gray;
            _status.UseMnemonic = false;

            var close = MakeButton("Close", 522, 386, 100);
            close.Click += (s, e) => Close();

            Controls.AddRange(new Control[]
            {
                heading, blurb, open, uriLabel, uriBox, copy, warn,
                idLabel, _clientId, _connect, _status, close
            });
        }

        static Button MakeButton(string text, int left, int top, int width)
        {
            var b = new Button
            {
                Text = text, Left = left, Top = top, Width = width, Height = 26,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(55, 55, 55),
                ForeColor = Color.Gainsboro
            };
            b.FlatAppearance.BorderColor = Color.FromArgb(80, 80, 80);
            return b;
        }

        void Go()
        {
            string id = _clientId.Text.Trim();
            if (id.Length == 0)
            {
                Say("Paste the Client ID from your Spotify app first.", true);
                return;
            }

            Settings.Set("spotify.client_id", id);
            _connect.Enabled = false;
            _clientId.Enabled = false;
            Say("Waiting for you to approve it in the browser...", false);

            // Connect blocks until the browser comes back, so it cannot run on
            // the UI thread or the window would freeze mid-sign-in.
            var worker = new System.Threading.Thread(() =>
            {
                string err;
                try { err = Playlists.Spotify.Connect(); }
                catch (Exception ex) { err = ex.Message; }

                BeginInvoke((Action)(() =>
                {
                    _connect.Enabled = true;
                    _clientId.Enabled = true;
                    if (err == null)
                    {
                        Say("Connected. Identified songs will be added to your " +
                            "\"Last.Pld\" playlist on Spotify.", false);
                        _connect.Text = "Reconnect";
                    }
                    else Say(err, true);
                }));
            });
            worker.IsBackground = true;
            worker.Start();
        }

        void Say(string message, bool bad)
        {
            _status.ForeColor = bad ? Color.FromArgb(240, 120, 120) : Color.FromArgb(140, 200, 140);
            _status.Text = message;
        }
    }

    // ---------------------------------------------------------------- facade

    static class Playlists
    {
        public static readonly SpotifyTarget Spotify = new SpotifyTarget();
        public static readonly AppleMusicTarget AppleMusic = new AppleMusicTarget();

        public static IPlaylistTarget[] All
        {
            get { return new IPlaylistTarget[] { Spotify, AppleMusic }; }
        }

        public static bool AutoAdd
        {
            get { return Settings.Get("auto_add", "1") != "0"; }
            set { Settings.Set("auto_add", value ? "1" : "0"); }
        }

        // Pushes to every connected target. Returns a human-readable summary,
        // or null if there was nothing to do.
        public static string AddEverywhere(TrackIds track)
        {
            if (track == null || !track.HasAnything) return null;

            var notes = new List<string>();
            foreach (var t in All)
            {
                if (!t.IsConnected) continue;
                string err = t.Add(track);
                notes.Add(err == null ? "Added to your " + t.Name + " playlist."
                                      : t.Name + ": " + err);
            }
            return notes.Count == 0 ? null : string.Join("\r\n", notes.ToArray());
        }
    }
}
