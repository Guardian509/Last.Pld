// Last.Pld — a local play-history recorder for Windows.
//
// Reads the System Media Transport Controls (SMTC) session that media apps
// publish to, and appends every track change to a CSV. This captures plays
// that apps themselves do not record — notably Apple Music radio stations,
// whose tracks never appear in the app's own History panel.
//
// Single exe. Runs in the tray, logs in the background, and opens a searchable
// history window on demand.
//
// Build (no SDK required):  run build.cmd, or by hand --
//   csc.exe /target:winexe /out:Last.Pld.exe
//     /reference:C:\Windows\System32\WinMetadata\Windows.Media.winmd
//     /reference:C:\Windows\System32\WinMetadata\Windows.Foundation.winmd
//     /reference:C:\Windows\System32\WinMetadata\Windows.Storage.winmd
//     /reference:<Framework64>\System.Runtime.dll
//     /reference:<Framework64>\System.Runtime.InteropServices.WindowsRuntime.dll
//     /reference:System.Windows.Forms.dll /reference:System.Drawing.dll
//     Last.Pld.cs
//
// That WindowsRuntime facade is not optional: without it, binding += to the
// SMTC events fails with a misleading CS1545 about calling add_X directly.
// It is a different assembly from System.Runtime.WindowsRuntime.dll, which
// must stay out -- that one drags in SDK union metadata this box lacks.
//
// Usage:
//   Last.Pld.exe              open the history window (and start logging)
//   Last.Pld.exe /background  start in the tray only, no window

using Microsoft.Win32;
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Windows.Foundation;
using Windows.Media.Control;

namespace LastPld
{
    // ---------------------------------------------------------------- model

    class Track
    {
        public DateTime Time;
        public string Title = "";
        public string Artist = "";
        public string Album = "";
        public string App = "";
        public DateTime Deleted;   // set only for entries sitting in the trash

        public string Source { get { return FriendlyApp(App); } }

        // AUMIDs are unreadable ("AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"),
        // so map the common ones and fall back to the package name.
        public static string FriendlyApp(string aumid)
        {
            if (string.IsNullOrEmpty(aumid)) return "";
            string a = aumid.ToLowerInvariant();
            if (a.Contains("applemusic")) return "Apple Music";
            if (a.Contains("itunes")) return "iTunes";
            if (a.Contains("spotify")) return "Spotify";
            if (a.Contains("msedge")) return "Edge";
            if (a.Contains("chrome")) return "Chrome";
            if (a.Contains("firefox")) return "Firefox";
            if (a.Contains("vlc")) return "VLC";
            if (a.Contains("appletv")) return "Apple TV";

            int cut = aumid.IndexOfAny(new[] { '_', '!' });
            string name = cut > 0 ? aumid.Substring(0, cut) : aumid;
            int dot = name.LastIndexOf('.');
            if (dot >= 0 && dot < name.Length - 1) name = name.Substring(dot + 1);
            return name.Replace(".exe", "");
        }
    }

    // ------------------------------------------------------------- storage

    static class Store
    {
        public static string Path;
        static readonly object Gate = new object();
        static readonly Encoding Utf8Bom = new UTF8Encoding(true);
        public static readonly List<Track> Items = new List<Track>();
        public static readonly List<Track> Trash = new List<Track>();
        public static string TrashPath;

        const string Header = "timestamp,title,artist,album,app";
        const string TrashHeader = "deleted,timestamp,title,artist,album,app";

        public static void Init(string path)
        {
            Path = path;
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path));

            // carry history over from the pre-rename logger, if present
            if (!File.Exists(path))
            {
                string legacy = System.IO.Path.Combine(
                    System.IO.Path.GetDirectoryName(path), "nowplaying.csv");
                try { if (File.Exists(legacy)) File.Copy(legacy, path); }
                catch { }
            }

            if (!File.Exists(path))
                File.AppendAllText(path, Header + "\r\n", Utf8Bom);

            TrashPath = System.IO.Path.Combine(
                System.IO.Path.GetDirectoryName(path), "lastpld.trash.csv");
            if (!File.Exists(TrashPath))
                File.AppendAllText(TrashPath, TrashHeader + "\r\n", Utf8Bom);

            Load();
            LoadTrash();
        }

        static void Load()
        {
            Items.Clear();
            string[] lines;
            try { lines = File.ReadAllLines(Path, Encoding.UTF8); }
            catch { return; }

            for (int i = 0; i < lines.Length; i++)
            {
                if (i == 0 && lines[i].StartsWith("timestamp", StringComparison.OrdinalIgnoreCase))
                    continue;
                if (lines[i].Length == 0) continue;

                var f = ParseCsvLine(lines[i]);
                if (f.Count < 5) continue;

                DateTime t;
                if (!DateTime.TryParseExact(f[0], "yyyy-MM-dd HH:mm:ss",
                        CultureInfo.InvariantCulture, DateTimeStyles.None, out t))
                    DateTime.TryParse(f[0], out t);

                Items.Add(new Track { Time = t, Title = f[1], Artist = f[2], Album = f[3], App = f[4] });
            }
        }

        static void LoadTrash()
        {
            Trash.Clear();
            string[] lines;
            try { lines = File.ReadAllLines(TrashPath, Encoding.UTF8); }
            catch { return; }

            for (int i = 0; i < lines.Length; i++)
            {
                if (i == 0 && lines[i].StartsWith("deleted", StringComparison.OrdinalIgnoreCase))
                    continue;
                if (lines[i].Length == 0) continue;

                var f = ParseCsvLine(lines[i]);
                if (f.Count < 6) continue;

                Trash.Add(new Track
                {
                    Deleted = ParseTime(f[0]),
                    Time = ParseTime(f[1]),
                    Title = f[2],
                    Artist = f[3],
                    Album = f[4],
                    App = f[5]
                });
            }
        }

        static DateTime ParseTime(string s)
        {
            DateTime t;
            if (DateTime.TryParseExact(s, "yyyy-MM-dd HH:mm:ss",
                    CultureInfo.InvariantCulture, DateTimeStyles.None, out t))
                return t;
            DateTime.TryParse(s, out t);
            return t;
        }

        // The UI reads on the message-loop thread while the logger appends on its
        // own; hand out snapshots so neither can trip over the other.
        public static Track[] Snapshot() { lock (Gate) return Items.ToArray(); }
        public static Track[] SnapshotTrash() { lock (Gate) return Trash.ToArray(); }

        public static void Delete(IEnumerable<Track> items)
        {
            lock (Gate)
            {
                foreach (var t in items)
                {
                    if (!Items.Remove(t)) continue;
                    t.Deleted = DateTime.Now;
                    Trash.Add(t);
                }
                RewriteHistory();
                RewriteTrash();
            }
        }

        public static void Restore(IEnumerable<Track> items)
        {
            lock (Gate)
            {
                foreach (var t in items)
                {
                    if (!Trash.Remove(t)) continue;
                    Items.Add(t);
                }
                Items.Sort((a, b) => a.Time.CompareTo(b.Time));
                RewriteHistory();
                RewriteTrash();
            }
        }

        public static void PurgeForever(IEnumerable<Track> items)
        {
            lock (Gate)
            {
                foreach (var t in items) Trash.Remove(t);
                RewriteTrash();
            }
        }

        public static void EmptyTrash()
        {
            lock (Gate) { Trash.Clear(); RewriteTrash(); }
        }

        static void RewriteHistory()
        {
            var sb = new StringBuilder();
            sb.Append(Header).Append("\r\n");
            foreach (var t in Items) sb.Append(Row(t)).Append("\r\n");
            try { File.WriteAllText(Path, sb.ToString(), Utf8Bom); }
            catch { }
        }

        static void RewriteTrash()
        {
            var sb = new StringBuilder();
            sb.Append(TrashHeader).Append("\r\n");
            foreach (var t in Trash)
                sb.Append(Csv(t.Deleted.ToString("yyyy-MM-dd HH:mm:ss")))
                  .Append(",").Append(Row(t)).Append("\r\n");
            try { File.WriteAllText(TrashPath, sb.ToString(), Utf8Bom); }
            catch { }
        }

        static string Row(Track t)
        {
            return string.Join(",",
                Csv(t.Time.ToString("yyyy-MM-dd HH:mm:ss")),
                Csv(t.Title), Csv(t.Artist), Csv(t.Album), Csv(t.App));
        }

        public static void Append(Track t)
        {
            lock (Gate)
            {
                Items.Add(t);
                try
                {
                    File.AppendAllText(Path, string.Join(",",
                        Csv(t.Time.ToString("yyyy-MM-dd HH:mm:ss")),
                        Csv(t.Title), Csv(t.Artist), Csv(t.Album), Csv(t.App)) + "\r\n", Utf8Bom);
                }
                catch { /* file open in Excel, disk busy — keep the in-memory copy */ }
            }
        }

        static string Csv(string s)
        {
            if (s == null) s = "";
            if (s.IndexOf(',') >= 0 || s.IndexOf('"') >= 0 || s.IndexOf('\n') >= 0)
                return "\"" + s.Replace("\"", "\"\"") + "\"";
            return s;
        }

        public static List<string> ParseCsvLine(string line)
        {
            var fields = new List<string>();
            var sb = new StringBuilder();
            bool quoted = false;

            for (int i = 0; i < line.Length; i++)
            {
                char c = line[i];
                if (quoted)
                {
                    if (c == '"')
                    {
                        if (i + 1 < line.Length && line[i + 1] == '"') { sb.Append('"'); i++; }
                        else quoted = false;
                    }
                    else sb.Append(c);
                }
                else if (c == '"') quoted = true;
                else if (c == ',') { fields.Add(sb.ToString()); sb.Length = 0; }
                else sb.Append(c);
            }
            fields.Add(sb.ToString());
            return fields;
        }
    }

    // -------------------------------------------------------------- filter

    // Decides what is worth recording. Browsers publish a media session for any
    // page with audio, so Facebook and Airbnb show up as "songs" with the page
    // title. No SMTC field separates them from real music — Facebook even
    // reports PlaybackType=Music — so browsers are excluded by default, with
    // YouTube allowed back in by matching the track against window titles.
    static class Filter
    {
        static readonly List<string> Allow = new List<string>();
        static bool _youtube = true;
        static string _configPath;

        static readonly string[] Defaults =
        {
            "# Last.Pld source filter - edit and restart to apply.",
            "#",
            "#   allow <text>   log any app whose id contains <text> (case-insensitive)",
            "#   youtube        also log browser audio when a YouTube window matches the track",
            "#   no-youtube     turn that off",
            "",
            "allow applemusic",
            "allow itunes",
            "allow spotify",
            "allow foobar",
            "allow musicbee",
            "allow aimp",
            "allow winamp",
            "allow vlc",
            "allow tidal",
            "allow deezer",
            "allow amazonmusic",
            "allow groove",
            "",
            "youtube",
        };

        public static void Init(string dir)
        {
            _configPath = Path.Combine(dir, "sources.txt");
            try
            {
                if (!File.Exists(_configPath))
                    File.WriteAllLines(_configPath, Defaults, new UTF8Encoding(true));

                foreach (var raw in File.ReadAllLines(_configPath, Encoding.UTF8))
                {
                    string line = raw.Trim();
                    if (line.Length == 0 || line.StartsWith("#")) continue;

                    if (line.Equals("youtube", StringComparison.OrdinalIgnoreCase)) _youtube = true;
                    else if (line.Equals("no-youtube", StringComparison.OrdinalIgnoreCase)) _youtube = false;
                    else if (line.StartsWith("allow ", StringComparison.OrdinalIgnoreCase))
                    {
                        string v = line.Substring(6).Trim();
                        if (v.Length > 0) Allow.Add(v.ToLowerInvariant());
                    }
                }
            }
            catch { }

            if (Allow.Count == 0)
            {
                Allow.Add("applemusic");
                Allow.Add("spotify");
                Allow.Add("itunes");
            }
        }

        public static bool ShouldLog(string app, string title)
        {
            string a = (app ?? "").ToLowerInvariant();
            foreach (var w in Allow)
                if (a.Contains(w)) return true;

            if (_youtube && IsBrowser(a)) return YouTubeWindowMatches(title);
            return false;
        }

        static bool IsBrowser(string a)
        {
            return a.Contains("chrome") || a.Contains("edge") || a.Contains("msedge")
                || a.Contains("firefox") || a.Contains("opera") || a.Contains("brave")
                || a.Contains("vivaldi");
        }

        // Require a window titled with BOTH "YouTube" and this track, so an
        // unrelated YouTube tab open elsewhere doesn't wave through Facebook.
        static bool YouTubeWindowMatches(string title)
        {
            if (string.IsNullOrEmpty(title)) return false;
            string probe = title.Length > 20 ? title.Substring(0, 20) : title;

            foreach (var t in WindowTitles())
            {
                if (t.IndexOf("YouTube", StringComparison.OrdinalIgnoreCase) < 0) continue;
                if (t.IndexOf(probe, StringComparison.OrdinalIgnoreCase) >= 0) return true;
            }
            return false;
        }

        delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")]
        static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int max);
        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hWnd);

        static List<string> WindowTitles()
        {
            var titles = new List<string>();
            try
            {
                EnumWindows((h, l) =>
                {
                    if (IsWindowVisible(h))
                    {
                        var sb = new StringBuilder(512);
                        if (GetWindowTextW(h, sb, sb.Capacity) > 0)
                            titles.Add(sb.ToString());
                    }
                    return true;
                }, IntPtr.Zero);
            }
            catch { }
            return titles;
        }
    }

    // -------------------------------------------------------------- logger

    class Logger
    {
        public event Action<Track> TrackLogged;
        public event Action<string> NowPlayingChanged;

        readonly Dictionary<string, string> _lastPerApp = new Dictionary<string, string>();
        readonly Dictionary<string, DateTime> _recent = new Dictionary<string, DateTime>();
        readonly HashSet<string> _hooked = new HashSet<string>();
        readonly object _pollGate = new object();
        GlobalSystemMediaTransportControlsSessionManager _mgr;
        string _lastHeader = "";
        volatile bool _stop;

        static string DupKey(string app, string title, string artist)
        {
            return app + "<|>" + title + "<~>" + artist;
        }

        public Logger()
        {
            // Seed from existing history so relaunching mid-track doesn't
            // record the in-progress song a second time.
            foreach (var t in Store.Items)
                _recent[DupKey(t.App, t.Title, t.Artist)] = t.Time;
        }

        public void Start()
        {
            var t = new Thread(Loop);
            t.IsBackground = true;
            t.Start();
        }

        public void Stop() { _stop = true; }

        void Loop()
        {
            while (!_stop)
            {
                try
                {
                    EnsureManager();
                    Poll();
                }
                catch { /* session torn down mid-read; retry next tick */ }

                // Events below are the fast path; this is only a safety net for
                // sessions that get recreated without firing SessionsChanged.
                Thread.Sleep(3000);
            }
        }

        // Detection used to lag by up to the full poll interval. Subscribing to
        // the session events makes it effectively immediate; the manager is now
        // created once instead of on every cycle.
        void EnsureManager()
        {
            if (_mgr == null)
            {
                _mgr = Await(GlobalSystemMediaTransportControlsSessionManager.RequestAsync());
                if (_mgr == null) return;
                // Needs System.Runtime.InteropServices.WindowsRuntime.dll referenced
                // for EventRegistrationToken, or these events won't bind.
                _mgr.SessionsChanged += (a, b) => { try { HookSessions(); Poll(); } catch { } };
            }
            HookSessions();
        }

        void HookSessions()
        {
            if (_mgr == null) return;
            foreach (var session in _mgr.GetSessions())
            {
                string app = session.SourceAppUserModelId ?? "";
                if (app.Length == 0 || _hooked.Contains(app)) continue;
                _hooked.Add(app);

                session.MediaPropertiesChanged += (a, b) => { try { Poll(); } catch { } };
                session.PlaybackInfoChanged += (a, b) => { try { Poll(); } catch { } };
            }
        }

        void Poll()
        {
            // events and the safety-net loop can both land here at once
            lock (_pollGate) { PollCore(); }
        }

        void PollCore()
        {
            var mgr = _mgr;
            if (mgr == null) return;

            // header reflects whichever session Windows considers current
            string header = "";
            try
            {
                var cur = mgr.GetCurrentSession();
                if (cur != null)
                {
                    var p = Await(cur.TryGetMediaPropertiesAsync());
                    if (p != null)
                    {
                        string ti, ar, al;
                        Split(p, out ti, out ar, out al);
                        if (ti.Length > 0 || ar.Length > 0)
                            header = ar.Length > 0 ? ti + "  —  " + ar : ti;
                    }
                }
            }
            catch { }

            if (header != _lastHeader)
            {
                _lastHeader = header;
                var h = NowPlayingChanged;
                if (h != null) h(header);
            }

            foreach (var session in mgr.GetSessions())
            {
                string app = session.SourceAppUserModelId ?? "";

                // Only record what is actually playing. Paused/stopped sessions
                // linger in the session list with stale metadata, and two live
                // sessions sharing an AUMID would otherwise ping-pong forever.
                try
                {
                    var info = session.GetPlaybackInfo();
                    if (info == null ||
                        info.PlaybackStatus != GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing)
                        continue;
                }
                catch { continue; }

                var props = Await(session.TryGetMediaPropertiesAsync());
                if (props == null) continue;

                string title, artist, album;
                Split(props, out title, out artist, out album);
                if (title.Length == 0 && artist.Length == 0) continue;
                if (!Filter.ShouldLog(app, title)) continue;

                string key = title + "\u0001" + artist;
                string prev;
                if (_lastPerApp.TryGetValue(app, out prev) && prev == key) continue;
                _lastPerApp[app] = key;

                // belt-and-braces: never record the same track from the same app
                // twice inside 90s, however the sessions churn underneath us
                string dupKey = DupKey(app, title, artist);
                DateTime seen;
                if (_recent.TryGetValue(dupKey, out seen) &&
                    (DateTime.Now - seen).TotalSeconds < 90) continue;
                _recent[dupKey] = DateTime.Now;

                var track = new Track
                {
                    Time = DateTime.Now,
                    Title = title,
                    Artist = artist,
                    Album = album,
                    App = app
                };
                Store.Append(track);

                var ev = TrackLogged;
                if (ev != null) ev(track);
            }
        }

        static void Split(GlobalSystemMediaTransportControlsSessionMediaProperties p,
                          out string title, out string artist, out string album)
        {
            title = p.Title ?? "";
            artist = p.Artist ?? "";
            album = p.AlbumTitle ?? "";

            // Apple Music packs "Artist — Album" into Artist and leaves AlbumTitle
            // empty, so split it back apart on the first em dash.
            if (album.Length == 0)
            {
                int sep = artist.IndexOf(" — ", StringComparison.Ordinal);
                if (sep > 0)
                {
                    album = artist.Substring(sep + 3);
                    artist = artist.Substring(0, sep);
                }
            }
        }

        // Block on a WinRT async op without AsTask(), which would require the
        // Windows SDK union metadata that isn't present on a stock machine.
        static T Await<T>(IAsyncOperation<T> op)
        {
            // 20ms steps added ~30ms to every property fetch for no reason
            int waited = 0;
            while (op.Status == AsyncStatus.Started && waited < 10000)
            {
                Thread.Sleep(2);
                waited += 2;
            }
            if (op.Status != AsyncStatus.Completed) return default(T);
            return op.GetResults();
        }
    }

    // ----------------------------------------------------------------- UI

    class MainForm : Form
    {
        readonly Logger _logger;
        readonly Label _now = new Label();
        readonly TextBox _search = new TextBox();
        readonly ComboBox _source = new ComboBox();
        readonly DataGridView _grid = new DataGridView();
        readonly Label _count = new Label();
        readonly Button _btnTrash;
        readonly Button _btnId;
        bool _viewTrash;
        bool _reallyExit;

        public MainForm(Logger logger)
        {
            _logger = logger;

            Text = "Last.Pld — play history";
            Width = 1120;
            Height = 620;
            StartPosition = FormStartPosition.CenterScreen;
            Icon = AppIcon.Get();
            BackColor = Color.FromArgb(32, 32, 32);
            ForeColor = Color.Gainsboro;
            Font = new Font("Segoe UI", 9f);

            // --- now playing banner
            _now.Dock = DockStyle.Top;
            _now.UseMnemonic = false;   // otherwise "Leave & Love" renders as "Leave _Love"
            _now.Height = 52;
            _now.TextAlign = ContentAlignment.MiddleLeft;
            _now.Padding = new Padding(14, 0, 0, 0);
            _now.Font = new Font("Segoe UI", 12f, FontStyle.Bold);
            _now.BackColor = Color.FromArgb(20, 20, 20);
            _now.ForeColor = Color.FromArgb(250, 90, 100);
            _now.Text = "Nothing playing";

            // --- filter bar
            var bar = new Panel { Dock = DockStyle.Top, Height = 44, BackColor = Color.FromArgb(32, 32, 32) };

            var lblFind = new Label
            {
                Text = "Search",
                Left = 14, Top = 14, Width = 48,
                ForeColor = Color.Gray
            };
            _search.Left = 66; _search.Top = 10; _search.Width = 260;
            _search.BackColor = Color.FromArgb(50, 50, 50);
            _search.ForeColor = Color.White;
            _search.BorderStyle = BorderStyle.FixedSingle;
            _search.TextChanged += (s, e) => Refill();

            _source.Left = 336; _source.Top = 10; _source.Width = 150;
            _source.DropDownStyle = ComboBoxStyle.DropDownList;
            _source.BackColor = Color.FromArgb(50, 50, 50);
            _source.ForeColor = Color.White;
            _source.FlatStyle = FlatStyle.Flat;
            _source.SelectedIndexChanged += (s, e) => Refill();

            _count.Left = 496; _count.Top = 14; _count.Width = 140;
            _count.ForeColor = Color.Gray;

            _btnTrash = MakeButton("Trash", 648);
            _btnTrash.Width = 104;
            _btnTrash.Click += (s, e) =>
            {
                _viewTrash = !_viewTrash;
                RebuildSources();
                Refill();
            };

            var btnCsv = MakeButton("Open CSV", 760);
            btnCsv.Click += (s, e) => TryOpen(Store.Path);
            var btnFolder = MakeButton("Folder", 856);
            btnFolder.Click += (s, e) => TryOpen(Path.GetDirectoryName(Store.Path));

            _btnId = MakeButton("Identify", 952);
            _btnId.Width = 110;
            _btnId.BackColor = Color.FromArgb(120, 40, 48);
            _btnId.Click += (s, e) => IdentifyNow();

            bar.Controls.AddRange(new Control[] { lblFind, _search, _source, _count, _btnTrash, btnCsv, btnFolder, _btnId });

            // --- grid
            _grid.Dock = DockStyle.Fill;
            _grid.ReadOnly = true;
            _grid.AllowUserToAddRows = false;
            _grid.AllowUserToDeleteRows = false;
            _grid.AllowUserToResizeRows = false;
            _grid.RowHeadersVisible = false;
            _grid.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
            _grid.MultiSelect = true;
            _grid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill;
            _grid.BackgroundColor = Color.FromArgb(40, 40, 40);
            _grid.BorderStyle = BorderStyle.None;
            _grid.EnableHeadersVisualStyles = false;
            _grid.ColumnHeadersDefaultCellStyle.BackColor = Color.FromArgb(24, 24, 24);
            _grid.ColumnHeadersDefaultCellStyle.ForeColor = Color.Gainsboro;
            _grid.ColumnHeadersDefaultCellStyle.Font = new Font("Segoe UI", 9f, FontStyle.Bold);
            _grid.ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.Single;
            _grid.DefaultCellStyle.BackColor = Color.FromArgb(40, 40, 40);
            _grid.DefaultCellStyle.ForeColor = Color.Gainsboro;
            _grid.DefaultCellStyle.SelectionBackColor = Color.FromArgb(70, 70, 70);
            _grid.DefaultCellStyle.SelectionForeColor = Color.White;
            _grid.GridColor = Color.FromArgb(55, 55, 55);
            _grid.RowTemplate.Height = 26;
            _grid.CellDoubleClick += (s, e) =>
            {
                if (_viewTrash) RestoreSelected(); else SearchAppleMusic();
            };
            _grid.KeyDown += (s, e) =>
            {
                if (e.KeyCode != Keys.Delete) return;
                e.Handled = true;
                if (_viewTrash) PurgeSelected(); else DeleteSelected();
            };

            _grid.Columns.Add(Col("When", 110));
            _grid.Columns.Add(Col("Title", 240));
            _grid.Columns.Add(Col("Artist", 220));
            _grid.Columns.Add(Col("Album", 200));
            _grid.Columns.Add(Col("Source", 110));

            var menu = new ContextMenuStrip();
            menu.Opening += (s, e) => BuildMenu(menu);
            _grid.ContextMenuStrip = menu;

            Controls.Add(_grid);
            Controls.Add(bar);
            Controls.Add(_now);

            // Esc clears the search if there is one, otherwise drops the window
            // back to the tray — the app keeps logging either way.
            KeyPreview = true;
            KeyDown += (s, e) =>
            {
                if (e.KeyCode == Keys.Escape)
                {
                    if (_search.Text.Length > 0) _search.Clear(); else Hide();
                    e.Handled = true;
                }
                else if (e.Control && e.KeyCode == Keys.F)
                {
                    _search.Focus();
                    _search.SelectAll();
                    e.Handled = true;
                }
            };

            RebuildSources();
            Refill();

            _logger.TrackLogged += t => BeginInvoke((Action)(() => { RebuildSources(); Refill(); }));
            _logger.NowPlayingChanged += h => BeginInvoke((Action)(() =>
                _now.Text = string.IsNullOrEmpty(h) ? "Nothing playing" : h));
        }

        Button MakeButton(string text, int left)
        {
            var b = new Button
            {
                Text = text, Left = left, Top = 9, Width = 88, Height = 26,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(55, 55, 55),
                ForeColor = Color.Gainsboro
            };
            b.FlatAppearance.BorderColor = Color.FromArgb(80, 80, 80);
            return b;
        }

        // Columns are in Fill mode, which ignores Width outright and splits the
        // row evenly — so "When" was as wide as "Title". FillWeight is the knob
        // that actually applies.
        static DataGridViewTextBoxColumn Col(string name, int weight)
        {
            return new DataGridViewTextBoxColumn
            {
                HeaderText = name,
                FillWeight = weight,
                SortMode = DataGridViewColumnSortMode.NotSortable
            };
        }

        void RebuildSources()
        {
            var seen = new List<string> { "All sources" };
            foreach (var t in Store.Snapshot())
                if (!seen.Contains(t.Source)) seen.Add(t.Source);
            foreach (var t in Store.SnapshotTrash())
                if (!seen.Contains(t.Source)) seen.Add(t.Source);

            if (_source.Items.Count == seen.Count) return;
            object keep = _source.SelectedItem;
            _source.Items.Clear();
            foreach (var s in seen) _source.Items.Add(s);
            _source.SelectedItem = (keep != null && seen.Contains(keep.ToString())) ? keep : seen[0];
        }

        void Refill()
        {
            string q = _search.Text.Trim();
            string src = _source.SelectedItem == null ? "All sources" : _source.SelectedItem.ToString();

            var all = _viewTrash ? Store.SnapshotTrash() : Store.Snapshot();

            // A track logging while you browse used to rebuild the grid under
            // you, throwing away the scroll position and the selection. Note
            // both, and put them back afterwards.
            int firstVisible = _grid.FirstDisplayedScrollingRowIndex;
            var wasSelected = new List<Track>();
            foreach (DataGridViewRow r in _grid.SelectedRows)
                if (r.Tag is Track) wasSelected.Add((Track)r.Tag);

            _grid.SuspendLayout();
            _grid.Rows.Clear();

            int shown = 0;
            // newest first
            for (int i = all.Length - 1; i >= 0 && shown < 5000; i--)
            {
                var t = all[i];
                if (src != "All sources" && t.Source != src) continue;
                if (q.Length > 0)
                {
                    string hay = t.Title + " " + t.Artist + " " + t.Album;
                    if (hay.IndexOf(q, StringComparison.OrdinalIgnoreCase) < 0) continue;
                }
                int r = _grid.Rows.Add(t.Time.ToString("MMM d  HH:mm"), t.Title, t.Artist, t.Album, t.Source);
                _grid.Rows[r].Tag = t;   // keep the identity, not just the text
                shown++;
            }

            if (wasSelected.Count > 0)
            {
                _grid.ClearSelection();
                foreach (DataGridViewRow r in _grid.Rows)
                    if (r.Tag is Track && wasSelected.Contains((Track)r.Tag))
                        r.Selected = true;
            }
            _grid.ResumeLayout();

            // After the layout is live again, not before — the scroll position
            // does not take while layout is suspended, and the setter throws if
            // the row can't be displayed.
            if (firstVisible > 0 && firstVisible < _grid.Rows.Count)
                try { _grid.FirstDisplayedScrollingRowIndex = firstVisible; } catch { }

            _count.Text = _viewTrash
                ? shown + " of " + all.Length + " in trash"
                : shown + " of " + all.Length + " tracks";
            _btnTrash.Text = _viewTrash ? "← History" : "Trash (" + Store.Trash.Count + ")";
            _grid.DefaultCellStyle.ForeColor = _viewTrash ? Color.DarkGray : Color.Gainsboro;
        }

        // ---- song identification (for sources that publish no metadata:
        // TikTok / Instagram / Facebook reels, games, streams) ----------------
        //
        // Records ~12s of system audio, downsamples it, and asks Shazam what it
        // is. Only a fingerprint is uploaded, never the recording itself.

        public void IdentifyNow()
        {
            if (!_btnId.Enabled) return;
            _btnId.Enabled = false;
            _btnId.Text = "Listening…";

            var worker = new Thread(() =>
            {
                string json;
                try { json = RunIdentify(); }
                catch (Exception ex) { json = "{\"matched\":false,\"error\":\"" + ex.Message + "\"}"; }

                // Push to the connected playlists here, on this thread: it is a
                // couple of network round trips and would visibly freeze the
                // window if it ran in the BeginInvoke below.
                string note = null;
                try
                {
                    if (Playlists.AutoAdd && !string.IsNullOrEmpty(JsonValue(json, "title")))
                        note = Playlists.AddEverywhere(TrackIdsFrom(json));
                }
                catch (Exception ex) { note = "Playlist: " + ex.Message; }

                BeginInvoke((Action)(() =>
                {
                    _btnId.Enabled = true;
                    _btnId.Text = "Identify";
                    ShowIdentifyResult(json, note);
                }));
            });
            worker.IsBackground = true;
            worker.Start();
        }

        static string RunIdentify()
        {
            string dir = Path.GetDirectoryName(Application.ExecutablePath);
            string raw = Path.Combine(Path.GetTempPath(), "lastpld_capture.wav");
            string clip = Path.Combine(Path.GetTempPath(), "lastpld_clip.wav");

            string loopcap = Path.Combine(dir, "loopcap.exe");
            string python = Path.Combine(dir, @"_build\venv312\Scripts\python.exe");
            string script = Path.Combine(dir, "recognize.py");

            if (!File.Exists(loopcap)) return Fail("loopcap.exe missing");
            if (!File.Exists(python)) return Fail("recognizer not installed");
            if (!File.Exists(script)) return Fail("recognize.py missing");

            // Clear both temp files up front. They used to be left behind, so a
            // failed capture would fall through to the previous run's clip and
            // confidently report the song from ten minutes ago.
            try { File.Delete(raw); } catch { }
            try { File.Delete(clip); } catch { }

            string err;
            RunProc(loopcap, "12 \"" + raw + "\"", 40000, out err);

            // WASAPI loopback yields no packets at all while the endpoint is
            // idle, so an empty file means silence, not a broken capture.
            if (!File.Exists(raw) || new FileInfo(raw).Length < 1024)
                return Fail("nothing was playing - no audio was captured");

            // 16 kHz mono is what fingerprinters expect
            try
            {
                RunProc("ffmpeg", "-hide_banner -loglevel error -y -i \"" + raw +
                                  "\" -ac 1 -ar 16000 -sample_fmt s16 \"" + clip + "\"", 40000, out err);
            }
            catch { return Fail("ffmpeg is not installed or not on PATH"); }
            if (!File.Exists(clip)) return Fail(err.Length > 0 ? LastLine(err) : "ffmpeg failed");

            string json = RunProc(python, "\"" + script + "\" \"" + clip + "\"", 60000, out err);
            if (json.Length == 0)
                return Fail(err.Length > 0 ? LastLine(err) : "the recognizer returned nothing");
            return json;
        }

        static string Fail(string message)
        {
            return "{\"matched\":false,\"error\":\"" +
                   message.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"}";
        }

        // Python puts the part worth reading on the last line of a traceback.
        static string LastLine(string s)
        {
            var lines = s.Split('\n');
            for (int i = lines.Length - 1; i >= 0; i--)
                if (lines[i].Trim().Length > 0) return lines[i].Trim();
            return s.Trim();
        }

        // Both pipes are drained asynchronously: reading one to the end while
        // the other fills its buffer is the classic way to deadlock here, and
        // ReadToEnd() before WaitForExit made the timeout unenforceable.
        static string RunProc(string exe, string args, int timeoutMs, out string stderr)
        {
            var psi = new System.Diagnostics.ProcessStartInfo(exe, args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;

            var so = new StringBuilder();
            var se = new StringBuilder();

            using (var p = System.Diagnostics.Process.Start(psi))
            {
                p.OutputDataReceived += (s, e) => { if (e.Data != null) so.AppendLine(e.Data); };
                p.ErrorDataReceived += (s, e) => { if (e.Data != null) se.AppendLine(e.Data); };
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();

                if (!p.WaitForExit(timeoutMs))
                {
                    try { p.Kill(); } catch { }
                    stderr = System.IO.Path.GetFileName(exe) + " timed out after " +
                             (timeoutMs / 1000) + "s";
                    return "";
                }

                p.WaitForExit();   // lets the async readers finish draining
                stderr = se.ToString().Trim();
                return so.ToString().Trim();
            }
        }

        static TrackIds TrackIdsFrom(string json)
        {
            return new TrackIds
            {
                Title = JsonValue(json, "title") ?? "",
                Artist = JsonValue(json, "artist") ?? "",
                Album = JsonValue(json, "album") ?? "",
                Isrc = JsonValue(json, "isrc") ?? "",
                AppleTrackId = JsonValue(json, "apple_track_id") ?? "",
                AppleMusicUrl = JsonValue(json, "apple_music_url") ?? ""
            };
        }

        void ShowIdentifyResult(string json, string playlistNote)
        {
            string title = JsonValue(json, "title");
            string artist = JsonValue(json, "artist");

            if (string.IsNullOrEmpty(title))
            {
                string err = JsonValue(json, "error");
                MessageBox.Show(
                    string.IsNullOrEmpty(err)
                        ? "No match.\r\n\r\nSped-up or pitch-shifted audio (common in reels) often defeats fingerprinting. Try again during a clearer part of the track."
                        : "Couldn't identify: " + err,
                    "Last.Pld", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            // record it like any other play, tagged so it is filterable
            Store.Append(new Track
            {
                Time = DateTime.Now,
                Title = title,
                Artist = artist ?? "",
                Album = JsonValue(json, "album") ?? "",
                App = "Shazam"
            });
            RebuildSources();
            Refill();

            string body = title + "\r\n" + (artist ?? "") + "\r\n\r\nAdded to your history.";
            if (!string.IsNullOrEmpty(playlistNote)) body += "\r\n" + playlistNote;

            MessageBox.Show(body, "Identified", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        // Minimal string-value reader; avoids dragging in a JSON dependency.
        static string JsonValue(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return null;
            string needle = "\"" + key + "\":";
            int i = json.IndexOf(needle, StringComparison.Ordinal);
            if (i < 0) return null;
            i += needle.Length;
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

        List<Track> SelectedTracks()
        {
            var list = new List<Track>();
            foreach (DataGridViewRow r in _grid.SelectedRows)
                if (r.Tag is Track) list.Add((Track)r.Tag);
            if (list.Count == 0 && _grid.CurrentRow != null && _grid.CurrentRow.Tag is Track)
                list.Add((Track)_grid.CurrentRow.Tag);
            return list;
        }

        // Deliberately unconfirmed: this is reversible, and the trash is the safety net.
        void DeleteSelected()
        {
            var sel = SelectedTracks();
            if (sel.Count == 0) return;
            Store.Delete(sel);
            RebuildSources();
            Refill();
        }

        void RestoreSelected()
        {
            var sel = SelectedTracks();
            if (sel.Count == 0) return;
            Store.Restore(sel);
            RebuildSources();
            Refill();
        }

        void PurgeSelected()
        {
            var sel = SelectedTracks();
            if (sel.Count == 0) return;
            if (MessageBox.Show(
                    "Permanently delete " + sel.Count + (sel.Count == 1 ? " entry" : " entries") +
                    "?\r\n\r\nThis cannot be undone.",
                    "Last.Pld", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                return;
            Store.PurgeForever(sel);
            Refill();
        }

        void EmptyTrashClicked()
        {
            if (Store.Trash.Count == 0) return;
            if (MessageBox.Show(
                    "Permanently delete all " + Store.Trash.Count + " entries in the trash?\r\n\r\nThis cannot be undone.",
                    "Last.Pld", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                return;
            Store.EmptyTrash();
            Refill();
        }

        void BuildMenu(ContextMenuStrip menu)
        {
            menu.Items.Clear();
            if (_viewTrash)
            {
                menu.Items.Add("Restore", null, (s, e) => RestoreSelected());
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add("Delete permanently  (Del)", null, (s, e) => PurgeSelected());
                menu.Items.Add("Empty trash", null, (s, e) => EmptyTrashClicked());
            }
            else
            {
                menu.Items.Add("Copy \"Artist — Title\"", null, (s, e) => CopyRow());
                menu.Items.Add("Search on Apple Music", null, (s, e) => SearchAppleMusic());
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add("Move to trash  (Del)", null, (s, e) => DeleteSelected());
            }
        }

        string SelectedCell(int index)
        {
            if (_grid.CurrentRow == null) return null;
            var v = _grid.CurrentRow.Cells[index].Value;
            return v == null ? "" : v.ToString();
        }

        void CopyRow()
        {
            string title = SelectedCell(1), artist = SelectedCell(2);
            if (title == null) return;
            string text = string.IsNullOrEmpty(artist) ? title : artist + " — " + title;
            try { Clipboard.SetText(text); } catch { }
        }

        void SearchAppleMusic()
        {
            string title = SelectedCell(1), artist = SelectedCell(2);
            if (string.IsNullOrEmpty(title)) return;
            string term = Uri.EscapeDataString((artist + " " + title).Trim());
            TryOpen("https://music.apple.com/us/search?term=" + term);
        }

        static void TryOpen(string target)
        {
            try { System.Diagnostics.Process.Start(target); }
            catch (Exception ex) { MessageBox.Show("Couldn't open:\r\n" + ex.Message); }
        }

        public void ExitForReal() { _reallyExit = true; Close(); }

        // Closing hides to tray so logging continues; Exit comes from the tray menu.
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!_reallyExit && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                Hide();
                return;
            }
            base.OnFormClosing(e);
        }
    }

    // ---------------------------------------------------------------- icon

    static class AppIcon
    {
        static Icon _icon;

        public static Icon Get()
        {
            if (_icon != null) return _icon;
            try
            {
                var bmp = new Bitmap(32, 32);
                using (var g = Graphics.FromImage(bmp))
                {
                    g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                    g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAlias;
                    g.Clear(Color.Transparent);
                    using (var b = new SolidBrush(Color.FromArgb(250, 90, 100)))
                    using (var f = new Font("Segoe UI Symbol", 22f, FontStyle.Bold))
                        g.DrawString("\u266B", f, b, new PointF(-1, 0));
                }
                _icon = Icon.FromHandle(bmp.GetHicon());
            }
            catch { _icon = SystemIcons.Application; }
            return _icon;
        }
    }

    // ---------------------------------------------------------------- main

    // ----------------------------------------------------------- autostart

    // One file, one double-click, running from every logon after that. The Run
    // key is per-user (HKCU), so this needs no elevation and only ever affects
    // the account that ran the exe.
    //
    // The value is rewritten on every launch, which is what makes the exe
    // portable: move it to another folder, run it once, and the entry follows
    // it. Deleting the exe stops the app - Windows silently skips a Run entry
    // whose target is missing - and leaves one inert registry value behind.
    // /uninstall removes that too.
    static class AutoStart
    {
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        const string ValueName = "Last.Pld";

        public static string Command
        {
            get { return "\"" + Application.ExecutablePath + "\" /background"; }
        }

        public static bool Installed
        {
            get
            {
                try
                {
                    using (var key = Registry.CurrentUser.OpenSubKey(RunKey))
                        return key != null && key.GetValue(ValueName) != null;
                }
                catch { return false; }
            }
        }

        public static void Install()
        {
            try
            {
                using (var key = Registry.CurrentUser.CreateSubKey(RunKey))
                    if (key != null) key.SetValue(ValueName, Command);
            }
            catch { }
        }

        public static void Remove()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                    if (key != null) key.DeleteValue(ValueName, false);
            }
            catch { }
        }

        // Keep the stored command pointing at wherever the exe actually is.
        public static void Refresh()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (key == null) return;
                    var current = key.GetValue(ValueName) as string;
                    if (current == null) return;
                    if (!string.Equals(current, Command, StringComparison.OrdinalIgnoreCase))
                        key.SetValue(ValueName, Command);
                }
            }
            catch { }
        }
    }

    static class Program
    {
        const string MutexName = "LastPld.SingleInstance.v1";
        const string ShowName = "LastPld.ShowWindow.v1";

        [STAThread]
        static void Main(string[] args)
        {
            bool background = false;
            bool uninstall = false;
            foreach (var a in args)
            {
                if (a.Equals("/background", StringComparison.OrdinalIgnoreCase)) background = true;
                if (a.Equals("/uninstall", StringComparison.OrdinalIgnoreCase)) uninstall = true;
            }

            if (uninstall)
            {
                AutoStart.Remove();
                MessageBox.Show(
                    "Last.Pld will no longer start when you log in.\r\n\r\n" +
                    "Your history is untouched. Delete Last.Pld.exe to remove " +
                    "the app itself.",
                    "Last.Pld", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            bool isFirst;
            var mutex = new Mutex(true, MutexName, out isFirst);

            // Already running? Ask that instance to surface, then exit.
            if (!isFirst)
            {
                try
                {
                    EventWaitHandle.OpenExisting(ShowName).Set();
                }
                catch { }
                return;
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string exeDir = Path.GetDirectoryName(Application.ExecutablePath);
            Store.Init(Path.Combine(exeDir, "lastpld.csv"));
            Filter.Init(exeDir);

            // First run sets itself to start at logon; later runs only correct
            // the path, so moving the exe does not leave a dead entry behind.
            if (AutoStart.Installed) AutoStart.Refresh();
            else AutoStart.Install();

            // Durable record of every real launch, so "did it start at logon?"
            // can be answered after the fact rather than guessed at.
            try
            {
                File.AppendAllText(
                    Path.Combine(exeDir, "startup.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "\t" +
                    (background ? "/background" : "window") + "\tpid=" +
                    System.Diagnostics.Process.GetCurrentProcess().Id + "\r\n",
                    new UTF8Encoding(true));
            }
            catch { }

            var logger = new Logger();
            var form = new MainForm(logger);

            // Force the window handle into existence now. Under /background the
            // form is never shown, so it had no handle, and every BeginInvoke
            // aimed at it threw — which silently killed the watcher thread below
            // and left "launch again to surface the window" permanently dead.
            { var forceHandle = form.Handle; }

            var tray = new NotifyIcon
            {
                Icon = AppIcon.Get(),
                Text = "Last.Pld — logging",
                Visible = true
            };

            // Hovering the tray icon should answer "what is playing?" without
            // opening anything. The shell truncates this at 63 characters.
            string trayIdle = tray.Text;
            logger.NowPlayingChanged += h =>
            {
                try
                {
                    form.BeginInvoke((Action)(() =>
                    {
                        string s = string.IsNullOrEmpty(h) ? trayIdle : h;
                        if (s.Length > 62) s = s.Substring(0, 59) + "...";
                        tray.Text = s;
                    }));
                }
                catch { }
            };

            var menu = new ContextMenuStrip();
            menu.Items.Add("Open history", null, (s, e) => Show(form));
            menu.Items.Add("Identify song now", null, (s, e) => { Show(form); form.IdentifyNow(); });

            // Rebuilt on open so it always shows the live connection state.
            var playlists = new ToolStripMenuItem("Playlists");
            menu.Opening += (s, e) => BuildPlaylistMenu(playlists);
            menu.Items.Add(playlists);
            menu.Items.Add("Open CSV folder", null, (s, e) =>
            {
                try { System.Diagnostics.Process.Start(Path.GetDirectoryName(Store.Path)); } catch { }
            });

            // Rebuilt on open, so the tick always shows the real registry state.
            var startup = new ToolStripMenuItem("Start at login");
            startup.CheckOnClick = true;
            menu.Opening += (s, e) => startup.Checked = AutoStart.Installed;
            startup.Click += (s, e) =>
            {
                if (startup.Checked) AutoStart.Install();
                else AutoStart.Remove();
            };
            menu.Items.Add(startup);

            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Exit", null, (s, e) =>
            {
                logger.Stop();
                tray.Visible = false;
                form.ExitForReal();
                Application.Exit();
            });
            tray.ContextMenuStrip = menu;
            tray.DoubleClick += (s, e) => Show(form);

            // second-instance launches raise this to surface the window
            var showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, ShowName);
            var watcher = new Thread(() =>
            {
                while (true)
                {
                    showEvent.WaitOne();
                    try { form.BeginInvoke((Action)(() => Show(form))); }
                    catch { return; }
                }
            });
            watcher.IsBackground = true;
            watcher.Start();

            logger.Start();

            if (background)
                Application.Run();       // tray only
            else
                Application.Run(form);   // window up front

            GC.KeepAlive(mutex);
        }

        static void BuildPlaylistMenu(ToolStripMenuItem root)
        {
            root.DropDownItems.Clear();

            var auto = new ToolStripMenuItem("Add identified songs to a playlist");
            auto.CheckOnClick = true;
            auto.Checked = Playlists.AutoAdd;
            auto.Click += (s, e) => Playlists.AutoAdd = auto.Checked;
            root.DropDownItems.Add(auto);
            root.DropDownItems.Add(new ToolStripSeparator());

            if (Playlists.Spotify.IsConnected)
            {
                var ok = new ToolStripMenuItem("Spotify - connected");
                ok.Enabled = false;
                root.DropDownItems.Add(ok);
                root.DropDownItems.Add("Reconnect Spotify...", null, (s, e) => ShowSpotifySetup());
                root.DropDownItems.Add("Disconnect Spotify", null, (s, e) =>
                {
                    Playlists.Spotify.Disconnect();
                    MessageBox.Show("Spotify disconnected. Nothing further will be added.",
                        "Last.Pld", MessageBoxButtons.OK, MessageBoxIcon.Information);
                });
            }
            else root.DropDownItems.Add("Connect Spotify...", null, (s, e) => ShowSpotifySetup());

            root.DropDownItems.Add(new ToolStripSeparator());
            root.DropDownItems.Add("Why not Apple Music?", null, (s, e) =>
                MessageBox.Show(Playlists.AppleMusic.Connect(), "Apple Music",
                    MessageBoxButtons.OK, MessageBoxIcon.Information));
        }

        static void ShowSpotifySetup()
        {
            using (var f = new SpotifySetupForm()) f.ShowDialog();
        }

        static void Show(Form f)
        {
            f.Show();
            if (f.WindowState == FormWindowState.Minimized)
                f.WindowState = FormWindowState.Normal;
            f.BringToFront();
            f.Activate();
        }
    }
}
