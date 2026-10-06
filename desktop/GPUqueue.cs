using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("GPUqueue Status")]
[assembly: AssemblyDescription("Read-only local GPU queue status and dashboard link")]
[assembly: AssemblyProduct("GPUqueue")]
[assembly: AssemblyVersion("1.2.0.0")]
[assembly: AssemblyFileVersion("1.2.0.0")]

namespace GPUqueueDesktop
{
    internal static class Data
    {
        internal static Dictionary<string, object> Parse(string value) { return new JavaScriptSerializer { MaxJsonLength = 8388608 }.Deserialize<Dictionary<string, object>>(value); }
        internal static object Get(Dictionary<string, object> d, string key) { object value; return d != null && d.TryGetValue(key, out value) ? value : null; }
        internal static string Text(Dictionary<string, object> d, string key) { return Convert.ToString(Get(d, key)) ?? ""; }
        internal static double Number(Dictionary<string, object> d, string key) { double value; return double.TryParse(Convert.ToString(Get(d, key)), out value) ? value : 0; }
        internal static bool Flag(Dictionary<string, object> d, string key) { return Get(d, key) is bool && (bool)Get(d, key); }
        internal static Dictionary<string, object> Map(Dictionary<string, object> d, string key) { return Get(d, key) as Dictionary<string, object>; }
        internal static double Now { get { return (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds; } }
        internal static string Safe(string value) { return (value ?? "").Replace("\r", " ").Replace("\n", " "); }
    }

    internal sealed class Settings
    {
        // Fixed local destinations: no executable, arguments or arbitrary URL
        // is accepted from a configuration file or command line.
        internal const string DashboardUrl = "http://127.0.0.1:11436/";
        internal readonly string Url = DashboardUrl.TrimEnd('/');
        internal readonly string State = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "GPUqueue", "state");
        internal static Settings Load()
        {
            return new Settings();
        }
    }

    internal sealed class Snapshot
    {
        internal Dictionary<string, object> Health, Status, Watchdog;
        internal string HealthError, StatusError, WatchdogError;
        internal double? CpuPercent;
    }

    internal static class Program
    {
        [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
        [STAThread]
        private static int Main(string[] args)
        {
            SetProcessDPIAware(); Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            string suffix = WindowsIdentity.GetCurrent().User.Value;
            bool first;
            using (var mutex = new Mutex(true, "Local\\GPUqueueDesktop-" + suffix, out first))
            using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\GPUqueueDesktopShow-" + suffix))
            {
                if (!first) { if (Array.IndexOf(args, "--tray") < 0) signal.Set(); return 0; }
                try
                {
                    using (var form = new QueueForm(Settings.Load(), false, signal))
                    {
                        if (Array.IndexOf(args, "--tray") >= 0) form.StartHidden = true;
                        Application.Run(form);
                    }
                    return 0;
                }
                catch (Exception) { MessageBox.Show("GPUqueue konnte nicht gestartet werden. Bitte die Installation prüfen. Die Queue wird dadurch nicht beendet.", "GPUqueue", MessageBoxButtons.OK, MessageBoxIcon.Warning); return 1; }
                finally { mutex.ReleaseMutex(); }
            }
        }
    }

    internal sealed class QueueForm : Form
    {
        private readonly Settings settings;
        private readonly bool testing;
        private readonly EventWaitHandle showEvent;
        private readonly HttpClient http;
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly NotifyIcon tray = new NotifyIcon();
        private readonly Dictionary<string, Icon> statusIcons = new Dictionary<string, Icon>();
        private readonly CpuSampler cpuSampler = new CpuSampler();
        private ToolStripMenuItem menuStatus, menuCpu, menuGpu, menuJobs;
        private string lastIconState;
        private Label headline, detail, gpu, telemetry, gpuCount, cpuCount, watchdog, backend, stamp, empty;
        private DataGridView jobs, cpuJobs;
        private Label cpuEmpty;
        private TabControl laneTabs;
        private ProgressBar memory;
        private bool quitting, polling, backendPolling;
        private int generation;
        internal bool StartHidden;
        private string visualState = "offline";
        private static readonly Color Ink = Color.FromArgb(31, 44, 61), Muted = Color.FromArgb(93, 109, 125), Green = Color.FromArgb(20, 133, 105), Amber = Color.FromArgb(176, 109, 19), Red = Color.FromArgb(187, 63, 73);

        internal QueueForm(Settings config, bool selfTest, EventWaitHandle signal)
        {
            settings = config; testing = selfTest; showEvent = signal;
            http = new HttpClient(new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false, UseCookies = false, UseDefaultCredentials = false }) { Timeout = TimeSpan.FromSeconds(3) };
            Text = "GPUqueue · Lokaler Überblick"; ClientSize = new Size(950, 650); MinimumSize = new Size(860, 560);
            ShowIcon = true; ShowInTaskbar = true;
            StartPosition = FormStartPosition.CenterScreen; BackColor = Color.FromArgb(242, 245, 249); ForeColor = Ink;
            Font = new Font("Segoe UI", 9F); AutoScaleMode = AutoScaleMode.Dpi;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(24, 18, 24, 18), ColumnCount = 1, RowCount = 8 };
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 44)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 62));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 100)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 46)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 24)); Controls.Add(layout);
            var title = LabelOf("GPUqueue", 23, Ink, true); layout.Controls.Add(title, 0, 0);
            var stateBox = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1, Margin = Padding.Empty };
            stateBox.RowStyles.Add(new RowStyle(SizeType.Absolute, 30)); stateBox.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            headline = LabelOf("Status wird geprüft …", 15, Muted, true); detail = LabelOf("Lokaler Coordinator · Nur integrierte Programme werden koordiniert.", 9, Muted, false);
            stateBox.Controls.Add(headline); stateBox.Controls.Add(detail); layout.Controls.Add(stateBox, 0, 1);
            var gpuBox = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Color.White, Padding = new Padding(12, 8, 12, 8), RowCount = 3, ColumnCount = 1 };
            gpuBox.RowStyles.Add(new RowStyle(SizeType.Absolute, 29)); gpuBox.RowStyles.Add(new RowStyle(SizeType.Absolute, 14)); gpuBox.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            gpu = LabelOf("GPU-Speicher —", 12, Ink, true); memory = new ProgressBar { Dock = DockStyle.Fill, Minimum = 0, Maximum = 1000, Margin = new Padding(0, 0, 0, 2) };
            telemetry = LabelOf("Noch keine Messung", 9, Muted, false); gpuBox.Controls.Add(gpu); gpuBox.Controls.Add(memory); gpuBox.Controls.Add(telemetry); layout.Controls.Add(gpuBox, 0, 2);
            laneTabs = new TabControl { Dock = DockStyle.Fill, Margin = new Padding(0, 7, 0, 7) };
            gpuCount = LabelOf("GPU  ·  —", 10, Ink, true); cpuCount = LabelOf("CPU  ·  —", 10, Ink, true);
            jobs = CreateJobsGrid(); cpuJobs = CreateJobsGrid();
            empty = LabelOf("Warte auf Coordinator …", 11, Muted, false);
            cpuEmpty = LabelOf("Warte auf Coordinator …", 11, Muted, false);
            laneTabs.TabPages.Add(CreateLaneTab("GPU", gpuCount, jobs, empty));
            laneTabs.TabPages.Add(CreateLaneTab("CPU", cpuCount, cpuJobs, cpuEmpty));
            layout.Controls.Add(laneTabs, 0, 3); layout.SetRowSpan(laneTabs, 2);
            var serviceBox = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
            watchdog = LabelOf("Überwachung: wird geprüft …", 9, Muted, false); backend = LabelOf("Ollama: wird geprüft …", 9, Muted, false); serviceBox.Controls.Add(watchdog); serviceBox.Controls.Add(backend); layout.Controls.Add(serviceBox, 0, 5);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.LeftToRight, WrapContents = false };
            actions.Controls.Add(ButtonOf("Webansicht öffnen", delegate { OpenWeb(); }, true)); actions.Controls.Add(ButtonOf("Jetzt aktualisieren", delegate { Poll(); }, false)); actions.Controls.Add(ButtonOf("Logs öffnen", delegate { OpenLogs(); }, false)); actions.Controls.Add(ButtonOf("Einstellungen", delegate { OpenSettings(); }, false)); layout.Controls.Add(actions, 0, 6);
            stamp = LabelOf("Schließen blendet dieses Fenster aus. Die Queue läuft weiter.", 8, Muted, false); layout.Controls.Add(stamp, 0, 7);
            var menu = new ContextMenuStrip();
            menuStatus = new ToolStripMenuItem("Status wird geprüft …") { Font = new Font(Font, FontStyle.Bold) };
            menuCpu = new ToolStripMenuItem("CPU gesamt: wird gemessen …");
            menuGpu = new ToolStripMenuItem("GPU gesamt: wird gemessen …");
            menuJobs = new ToolStripMenuItem("Queue: wird geprüft …");
            menu.Items.AddRange(new ToolStripItem[] { menuStatus, menuGpu, menuJobs, menuCpu, new ToolStripSeparator() });
            menu.Items.Add("Übersicht öffnen", null, delegate { Reveal(); }); menu.Items.Add("Webansicht öffnen", null, delegate { OpenWeb(); }); menu.Items.Add("Logs öffnen", null, delegate { OpenLogs(); }); menu.Items.Add("Einstellungen …", null, delegate { OpenSettings(); }); menu.Items.Add(new ToolStripSeparator()); menu.Items.Add("Tray beenden (Queue läuft weiter)", null, delegate { quitting = true; Close(); });
            menu.Opening += delegate { Poll(); };
            tray.ContextMenuStrip = menu; tray.MouseClick += delegate(object sender, MouseEventArgs e) { if (e.Button == MouseButtons.Left) Reveal(); }; SetIcon("offline"); tray.Visible = !testing;
            timer.Interval = 1000; int ticks = 0; timer.Tick += delegate { if (showEvent != null && showEvent.WaitOne(0)) Reveal(); if (++ticks % 4 == 0) Poll(); };
            Shown += delegate { if (StartHidden) Hide(); if (!testing) { timer.Start(); Poll(); } };
            FormClosing += delegate(object sender, FormClosingEventArgs e) { if (!quitting && !testing && e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; Hide(); } };
        }
        private Label LabelOf(string text, float size, Color color, bool bold) { return new Label { Text = text, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true, Margin = Padding.Empty, ForeColor = color, Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular) }; }
        private Button ButtonOf(string text, EventHandler handler, bool primary) { var b = new Button { Text = text, AutoSize = true, Height = 34, Padding = new Padding(10, 4, 10, 4), Margin = new Padding(0, 4, 8, 0), FlatStyle = FlatStyle.Flat, BackColor = primary ? Green : Color.White, ForeColor = primary ? Color.White : Ink }; b.FlatAppearance.BorderColor = primary ? Green : Color.FromArgb(207, 216, 226); b.Click += handler; return b; }
        private DataGridView CreateJobsGrid()
        {
            var grid = new DataGridView { Dock = DockStyle.Fill, BackgroundColor = Color.White, BorderStyle = BorderStyle.None, ReadOnly = true, AllowUserToAddRows = false, AllowUserToDeleteRows = false, AllowUserToResizeRows = false, RowHeadersVisible = false, AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill, SelectionMode = DataGridViewSelectionMode.FullRowSelect, MultiSelect = false, EnableHeadersVisualStyles = false, GridColor = Color.FromArgb(229, 234, 240), CellBorderStyle = DataGridViewCellBorderStyle.SingleHorizontal, ColumnHeadersHeight = 34, ColumnHeadersHeightSizeMode = DataGridViewColumnHeadersHeightSizeMode.DisableResizing };
            grid.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(226, 233, 241), ForeColor = Ink, Font = new Font(Font, FontStyle.Bold), Padding = new Padding(5) };
            grid.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.White, ForeColor = Ink, SelectionBackColor = Color.FromArgb(223, 240, 235), SelectionForeColor = Ink, Padding = new Padding(5) }; grid.RowTemplate.Height = 42;
            string[] names = { "lane", "owner", "state", "reason", "time" }, labels = { "Spur", "Programm / Modell", "Status", "Grund / Skript", "Zeit" };
            int[] weights = { 8, 29, 16, 30, 17 };
            for (int i = 0; i < names.Length; i++) grid.Columns.Add(new DataGridViewTextBoxColumn { Name = names[i], HeaderText = labels[i], FillWeight = weights[i], SortMode = DataGridViewColumnSortMode.NotSortable });
            return grid;
        }
        private TabPage CreateLaneTab(string name, Label count, DataGridView grid, Label placeholder)
        {
            var tab = new TabPage(name) { BackColor = Color.White, Padding = new Padding(8) };
            var rows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
            rows.RowStyles.Add(new RowStyle(SizeType.Absolute, 36)); rows.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            var panel = new Panel { Dock = DockStyle.Fill };
            placeholder.TextAlign = ContentAlignment.MiddleCenter;
            panel.Controls.Add(grid); panel.Controls.Add(placeholder);
            rows.Controls.Add(count, 0, 0); rows.Controls.Add(panel, 0, 1); tab.Controls.Add(rows);
            return tab;
        }
        internal void Reveal() { StartHidden = false; Show(); WindowState = FormWindowState.Normal; Activate(); BringToFront(); }

        private async Task<Dictionary<string, object>> Get(string route) { string json = await http.GetStringAsync(settings.Url + route).ConfigureAwait(false); return Data.Parse(json); }
        private async Task FetchHealth(Snapshot s) { try { s.Health = await Get("/health").ConfigureAwait(false); } catch { s.HealthError = "Coordinator nicht erreichbar"; } }
        private async Task FetchStatus(Snapshot s) { try { s.Status = await Get("/status").ConfigureAwait(false); } catch { s.StatusError = "Statusabfrage fehlgeschlagen"; } }
        private async void Poll()
        {
            if (testing || polling || IsDisposed) return; polling = true;
            try
            {
                var snapshot = new Snapshot();
                await Task.WhenAll(FetchHealth(snapshot), FetchStatus(snapshot), Task.Run(delegate {
                    snapshot.CpuPercent = cpuSampler.Sample();
                    try { snapshot.Watchdog = Data.Parse(File.ReadAllText(Path.Combine(settings.State, "watchdog.json"), Encoding.UTF8)); }
                    catch { snapshot.WatchdogError = "Statusdatei nicht verfügbar"; }
                }));
                if (IsDisposed) return; Apply(snapshot); generation++;
                if (snapshot.Health != null && Data.Text(snapshot.Health, "service") == "local-gpu-coordinator") PollBackend(generation);
                else backend.Text = "Ollama: nicht geprüft (Coordinator nicht erreichbar)";
            }
            finally { polling = false; }
        }
        private async void PollBackend(int version)
        {
            if (backendPolling || IsDisposed) return; backendPolling = true;
            try { var response = await Get("/api/version"); if (!IsDisposed && version == generation) backend.Text = String.IsNullOrWhiteSpace(Data.Text(response, "version")) ? "Ollama: unerwartete Antwort" : "Ollama: erreichbar · Version " + Data.Safe(Data.Text(response, "version")); }
            catch { if (!IsDisposed && version == generation) backend.Text = "Ollama: nicht erreichbar / Abfrage fehlgeschlagen"; }
            finally { backendPolling = false; }
        }

        internal void Apply(Snapshot s)
        {
            bool reachable = s.Health != null && Data.Text(s.Health, "service") == "local-gpu-coordinator";
            bool valid = reachable && s.Status != null && Data.Get(s.Status, "jobs") is IList && Data.Map(s.Status, "gpu") != null;
            bool worker = reachable && Data.Flag(s.Health, "worker_alive"); bool paused = valid && Data.Flag(s.Status, "paused");
            int[] active = new int[2], queued = new int[2], blocked = new int[2];
            jobs.Rows.Clear(); cpuJobs.Rows.Clear();
            if (valid)
            {
                foreach (object item in (IEnumerable)Data.Get(s.Status, "jobs"))
                {
                    var job = item as Dictionary<string, object>; if (job == null) continue; string state = Data.Text(job, "state");
                    if (state == "completed" || state == "failed" || state == "interrupted" || state == "cancelled") continue;
                    int lane = Data.Text(job, "lane") == "cpu" ? 1 : 0;
                    if (state == "queued") queued[lane]++; else if (state == "recovery_blocked") blocked[lane]++; else active[lane]++;
                    string owner = Data.Safe(Data.Text(job, "owner")), model = Data.Safe(Data.Text(job, "model")), reason = Data.Safe(Data.Text(job, "reason")), script = Data.Safe(Data.Text(job, "script"));
                    string time = state == "queued" ? "Wartet " + Duration(Data.Number(job, "wait_seconds")) : "Lauf " + Duration(Data.Number(job, "run_seconds"));
                    var grid = lane == 1 ? cpuJobs : jobs;
                    int index = grid.Rows.Add(lane == 1 ? "CPU" : "GPU", owner + (model.Length > 0 ? " · " + model : ""), StateName(state), reason + (script.Length > 0 && script != "not_supplied" ? (reason.Length > 0 ? " · " : "") + script : ""), time);
                    foreach (DataGridViewCell cell in grid.Rows[index].Cells) cell.ToolTipText = Convert.ToString(cell.Value);
                    if (state == "recovery_blocked") grid.Rows[index].DefaultCellStyle.ForeColor = Amber;
                }
                gpuCount.Text = Counter("GPU", active[0], queued[0], blocked[0]); cpuCount.Text = Counter("CPU", active[1], queued[1], blocked[1]);
                var g = Data.Map(s.Status, "gpu"); double total = Data.Number(g, "total_mb"), used = Data.Number(g, "used_mb"), age = Math.Max(0, Data.Now - Data.Number(g, "observed_at"));
                if (total > 0)
                {
                    gpu.Text = String.Format("GPU-Speicher  {0:0.0} / {1:0.0} GB  ·  {2:0.0} GB frei", used / 1024, total / 1024, Data.Number(g, "free_mb") / 1024);
                    memory.Value = (int)Math.Max(0, Math.Min(1000, 1000 * used / total)); memory.Enabled = age <= 15;
                    telemetry.Text = Data.Safe(Data.Text(g, "name")) + "  ·  " + Data.Number(g, "utilization").ToString("0") + "% Auslastung  ·  " + (age > 15 ? "VERALTET · " : "Messung vor ") + Duration(age);
                    telemetry.ForeColor = age > 15 ? Amber : Muted;
                }
                else ClearGPU("GPU-Messung nicht verfügbar");
            }
            else { gpuCount.Text = "GPU  ·  unbekannt"; cpuCount.Text = "CPU  ·  unbekannt"; ClearGPU("Keine aktuelle GPU-Messung"); }
            cpuCount.ForeColor = blocked[1] > 0 ? Amber : Ink;
            int activeTotal = active[0], waitingTotal = queued[0]; bool hasBlocked = blocked[0] > 0;
            visualState = !reachable ? "offline" : !valid || !worker || hasBlocked ? "blocked" : paused ? "paused" : activeTotal > 0 ? "busy" : waitingTotal > 0 ? "waiting" : "online";
            headline.Text = !reachable ? "Offline · Coordinator nicht erreichbar" : !valid ? "Erreichbar · Status nicht verfügbar" : !worker ? "Blockiert · Worker nicht aktiv" : hasBlocked ? "GPU blockiert · Klärung erforderlich" : paused ? "Pausiert · Neue Starts angehalten" : activeTotal > 0 ? "Läuft · " + activeTotal + " aktive GPU-Aufträge" : waitingTotal > 0 ? "Wartet · " + waitingTotal + " GPU-Aufträge in der Queue" : "Bereit · GPU-Queue frei";
            headline.ForeColor = StateColor(visualState);
            detail.Text = !reachable ? "Verbindung wird automatisch erneut geprüft. Frühere Jobdaten wurden ausgeblendet." : !valid ? "Lesende Statusabfrage fehlgeschlagen. Frühere Jobdaten wurden ausgeblendet." : hasBlocked ? "Ein GPU-Auftrag wartet auf Prüfung. Details stehen in der Webansicht." : paused ? "Aktive Jobs können weiterlaufen. Startfreigabe über die Webansicht." : "Ampel: GPU-Queue · CPU-Aufträge im eigenen Tab · Nur integrierte Programme werden erfasst.";
            empty.Visible = jobs.Rows.Count == 0; empty.Text = valid ? "Keine aktiven oder wartenden GPU-Aufträge" : "Jobstatus derzeit nicht verfügbar"; if (empty.Visible) empty.BringToFront();
            cpuEmpty.Visible = cpuJobs.Rows.Count == 0; cpuEmpty.Text = valid ? "Keine aktiven oder wartenden CPU-Aufträge" : "Jobstatus derzeit nicht verfügbar"; if (cpuEmpty.Visible) cpuEmpty.BringToFront();
            double observed = Data.Number(s.Watchdog, "observed_at"); double watchAge = Data.Now - observed; string watchState = Data.Text(s.Watchdog, "status");
            watchdog.Text = observed <= 0 ? "Überwachung: Statusdatei nicht verfügbar" : "Überwachung: " + (watchAge > 40 ? "VERALTET · " : "") + WatchName(watchState) + " · vor " + Duration(Math.Max(0, watchAge)) + (Data.Text(s.Watchdog, "reason").Length > 0 ? " · " + Data.Safe(Data.Text(s.Watchdog, "reason")) : "");
            watchdog.ForeColor = observed <= 0 || watchAge > 40 ? Amber : Muted;
            menuStatus.Text = headline.Text; menuStatus.ForeColor = StateColor(visualState);
            menuCpu.Text = s.CpuPercent.HasValue ? "CPU gesamt: " + s.CpuPercent.Value.ToString("0") + " %" : "CPU gesamt: Messung nicht verfügbar";
            var menuGpuData = valid ? Data.Map(s.Status, "gpu") : null;
            bool gpuFresh = menuGpuData != null && Data.Get(menuGpuData, "utilization") != null && Data.Number(menuGpuData, "observed_at") > 0 && Data.Now - Data.Number(menuGpuData, "observed_at") <= 15;
            menuGpu.Text = gpuFresh ? String.Format("GPU gesamt: {0:0} % · VRAM {1:0.0} / {2:0.0} GB", Data.Number(menuGpuData, "utilization"), Data.Number(menuGpuData, "used_mb") / 1024, Data.Number(menuGpuData, "total_mb") / 1024) : "GPU gesamt: keine aktuelle Messung";
            menuJobs.Text = valid ? "GPU-Queue: " + activeTotal + " aktiv · " + waitingTotal + " wartend · " + blocked[0] + " blockiert" : "GPU-Queue: Status unbekannt";
            stamp.Text = "Aktualisiert " + DateTime.Now.ToString("HH:mm:ss") + "  ·  Schließen blendet aus; die Queue läuft weiter.";
            SetIcon(visualState);
        }
        private static string Counter(string name, int active, int waiting, int blocked) { return name + "  ·  " + active + " aktiv  ·  " + waiting + " wartend" + (blocked > 0 ? "  ·  " + blocked + " blockiert" : ""); }
        private static string Duration(double seconds) { if (seconds < 60) return Math.Max(0, seconds).ToString("0") + " s"; if (seconds < 3600) return (seconds / 60).ToString("0") + " min"; return (seconds / 3600).ToString("0.0") + " h"; }
        private static string StateName(string state) { switch (state) { case "running": return "Läuft"; case "queued": return "Wartet"; case "preparing": return "Vorbereitung"; case "draining": return "Freigabe"; case "cancelling": return "Wird beendet"; case "recovery_blocked": return "Blockiert"; default: return Data.Safe(state); } }
        private static string WatchName(string state) { switch (state) { case "healthy": case "running": return "aktiv"; case "starting": return "Start läuft"; case "backoff": return "Wiederanlauf wartet"; case "blocked": return "blockiert"; default: return Data.Safe(state); } }
        private void ClearGPU(string message) { gpu.Text = "GPU-Speicher  —"; telemetry.Text = message; telemetry.ForeColor = Muted; memory.Value = 0; memory.Enabled = false; }
        [DllImport("user32.dll")] private static extern bool DestroyIcon(IntPtr handle);
        internal static Color StateColor(string state) { return state == "online" ? Green : state == "busy" || state == "waiting" || state == "paused" ? Amber : Red; }
        private void SetIcon(string state)
        {
            if (lastIconState == state) { if (!testing) tray.Visible = true; return; }
            Color color = StateColor(state);
            Icon next;
            if (!statusIcons.TryGetValue(state, out next))
            {
            using (var bitmap = new Bitmap(32, 32)) using (Graphics g = Graphics.FromImage(bitmap))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias; g.Clear(Color.Transparent);
                using (var brush = new SolidBrush(Color.FromArgb(31, 44, 61))) g.FillEllipse(brush, 1, 1, 30, 30);
                using (var pen = new Pen(Color.White, 2)) { g.DrawRectangle(pen, 9, 8, 13, 14); g.DrawLine(pen, 12, 12, 19, 12); g.DrawLine(pen, 12, 17, 19, 17); }
                using (var brush = new SolidBrush(color)) g.FillEllipse(brush, 20, 20, 12, 12);
                IntPtr handle = bitmap.GetHicon(); next = (Icon)Icon.FromHandle(handle).Clone(); DestroyIcon(handle);
                statusIcons.Add(state, next);
            }
            }
            tray.Icon = next; Icon = next; lastIconState = state;
            if (!testing) tray.Visible = true;
            tray.Text = state == "online" ? "GPUqueue · Bereit" : state == "busy" ? "GPUqueue · Auftrag läuft" : state == "waiting" ? "GPUqueue · Wartet" : state == "paused" ? "GPUqueue · Pausiert" : state == "blocked" ? "GPUqueue · Prüfung erforderlich" : "GPUqueue · Offline";
        }
        private void OpenSettings()
        {
            using (var dialog = new Form { Text = "GPUqueue · Einstellungen", StartPosition = FormStartPosition.CenterParent, ClientSize = new Size(570, 215), FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false, MinimizeBox = false, Icon = Icon, Font = Font })
            {
                var toggle = new CheckBox { Text = "Status-App bei Windows-Anmeldung starten", Left = 22, Top = 22, Width = 525, Height = 30, Enabled = false };
                var explanation = new Label { Text = "Gilt nur für das Tray-Symbol. Die Queue und ihre Hintergrundüberwachung laufen unabhängig weiter. Kein zusätzlicher Eintrag im Autostart-Ordner nötig.", Left = 22, Top = 62, Width = 525, Height = 54 };
                var result = new Label { Text = "Autostart wird geprüft …", Left = 22, Top = 126, Width = 525, Height = 30 };
                var close = new Button { Text = "Schließen", Left = 440, Top = 169, Width = 105, DialogResult = DialogResult.OK };
                dialog.Controls.AddRange(new Control[] { toggle, explanation, result, close }); dialog.CancelButton = close;
                bool loaded = false;
                dialog.Shown += async delegate {
                    try { bool value = await Task.Run(() => TrayAutostart.GetEnabled()); if (dialog.IsDisposed) return; toggle.Checked = value; loaded = true; toggle.Enabled = true; result.Text = value ? "Autostart ist eingeschaltet." : "Autostart ist ausgeschaltet."; }
                    catch { if (!dialog.IsDisposed) result.Text = "Autostart-Aufgabe fehlt oder ist nicht zugänglich. Installation prüfen."; }
                };
                toggle.CheckedChanged += async delegate {
                    if (!loaded) return;
                    bool requested = toggle.Checked; toggle.Enabled = false; result.Text = "Wird gespeichert …";
                    try { bool actual = await Task.Run(() => TrayAutostart.SetEnabled(requested)); if (dialog.IsDisposed) return; loaded = false; toggle.Checked = actual; loaded = true; result.Text = actual ? "Autostart ist eingeschaltet." : "Autostart ist ausgeschaltet."; }
                    catch { if (!dialog.IsDisposed) { loaded = false; toggle.Checked = !requested; loaded = true; result.Text = "Änderung nicht bestätigt. Einstellungen erneut öffnen und prüfen."; } }
                    finally { if (!dialog.IsDisposed) toggle.Enabled = true; }
                };
                dialog.ShowDialog(this);
            }
        }
        private void OpenWeb()
        {
            if (testing) return;
            try
            {
                // User-clicked, fixed HTTP link in the default browser. No
                // interpreter, shell command, login ticket or control token.
                Process.Start(new ProcessStartInfo(Settings.DashboardUrl) { UseShellExecute = true });
            }
            catch { if (!IsDisposed) MessageBox.Show(this, "Die Webansicht konnte nicht geöffnet werden. Adresse: " + Settings.DashboardUrl, "GPUqueue"); }
        }
        private void OpenLogs()
        {
            if (testing) return;
            using (var viewer = new Form { Text = "GPUqueue · Lokale Protokolle (nur lesen)", StartPosition = FormStartPosition.CenterParent, Size = new Size(850, 550), MinimumSize = new Size(650, 400), Font = Font })
            {
                var tabs = new TabControl { Dock = DockStyle.Fill };
                foreach (string name in new[] { "watchdog.log", "server.stderr.log", "server.stdout.log" })
                {
                    var tab = new TabPage(name);
                    var text = new TextBox { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, Font = new Font("Consolas", 9F) };
                    try
                    {
                        using (var file = new FileStream(Path.Combine(settings.State, name), FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                        {
                            bool truncated = file.Length > 65536;
                            if (truncated) file.Seek(-65536, SeekOrigin.End);
                            using (var reader = new StreamReader(file, Encoding.UTF8))
                            {
                                if (truncated) reader.ReadLine();
                                text.Text = (truncated ? "[Auszug: letzte 64 KB]" + Environment.NewLine : "") + reader.ReadToEnd();
                            }
                        }
                    }
                    catch { text.Text = "Protokoll noch nicht vorhanden oder derzeit nicht lesbar."; }
                    tab.Controls.Add(text); tabs.TabPages.Add(tab);
                }
                viewer.Controls.Add(tabs); viewer.ShowDialog(this);
            }
        }
        protected override void Dispose(bool disposing) { if (disposing) { timer.Stop(); timer.Dispose(); tray.Visible = false; tray.Dispose(); foreach (Icon icon in statusIcons.Values) icon.Dispose(); statusIcons.Clear(); http.Dispose(); } base.Dispose(disposing); }

    }
}
