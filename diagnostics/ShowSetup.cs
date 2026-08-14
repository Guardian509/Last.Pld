// Renders SpotifySetupForm on its own so the layout can be eyeballed without
// driving the tray menu. Build with Last.Pld.cs + Playlists.cs and
// /main:LastPld.ShowSetup.
using System;
using System.Windows.Forms;

namespace LastPld
{
    static class ShowSetup
    {
        [STAThread]
        static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SpotifySetupForm());
        }
    }
}
