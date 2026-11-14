using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
        using (var hash = SHA256.Create())
        {
            var digest = hash.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant()));
            string project = "research-agent-" + BitConverter.ToString(digest, 0, 8).Replace("-", "").ToLowerInvariant();
            bool first;
            using (var mutex = new Mutex(true, @"Local\" + project, out first))
            {
                if (!first)
                {
                    MessageBox.Show("Research Agent уже есть в области уведомлений Windows.", "Research Agent");
                    return;
                }
                using (var tray = new AgentTray(root, project))
                    Application.Run(tray);
            }
        }
    }
}

internal sealed class AgentTray : ApplicationContext
{
    private const string Address = "http://127.0.0.1:8765/";
    private readonly string root;
    private readonly string project;
    private readonly string docker;
    private readonly string dockerDesktop;
    private readonly NotifyIcon tray;
    private readonly ContextMenuStrip menu;
    private readonly ToolStripMenuItem status, start, stop, open, exit;
    private readonly System.Windows.Forms.Timer timer;
    private readonly Icon gray = StatusIcon(Color.FromArgb(138, 146, 151));
    private readonly Icon yellow = StatusIcon(Color.FromArgb(252, 193, 72));
    private readonly Icon green = StatusIcon(Color.FromArgb(78, 218, 142));
    private readonly Icon red = StatusIcon(Color.FromArgb(243, 126, 91));
    private bool busy, checking, managed, healthy;

    public AgentTray(string root, string project)
    {
        this.root = root;
        this.project = project;
        string installation = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs", "DockerDesktop");
        if (!File.Exists(Path.Combine(installation, "Docker Desktop.exe")))
            installation = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Docker", "Docker");
        dockerDesktop = Path.Combine(installation, "Docker Desktop.exe");
        docker = Path.Combine(installation, "resources", "bin", "docker.exe");
        if (!File.Exists(docker)) docker = "docker.exe";

        menu = new ContextMenuStrip();
        status = new ToolStripMenuItem { Enabled = false };
        open = new ToolStripMenuItem("Открыть Research Agent", null, (s, e) => Open(Address));
        start = new ToolStripMenuItem("Запустить сервер", null, async (s, e) => await StartServer());
        stop = new ToolStripMenuItem("Остановить сервер", null, async (s, e) => await StopServer());
        exit = new ToolStripMenuItem("Закрыть значок", null, (s, e) => ExitThread());
        menu.Items.AddRange(new ToolStripItem[] { status, new ToolStripSeparator(), open, start, stop,
            new ToolStripMenuItem("Открыть папку Materials", null, (s, e) => OpenMaterials()),
            new ToolStripSeparator(), exit });
        tray = new NotifyIcon { ContextMenuStrip = menu, Icon = gray, Text = "Research Agent", Visible = true };
        tray.DoubleClick += (s, e) => { if (open.Enabled) Open(Address); };
        ShowStatus("Остановлен", gray, false);
        timer = new System.Windows.Forms.Timer { Interval = 2500 };
        timer.Tick += CheckServer;
        Application.Idle += StartOnIdle;
        timer.Start();
    }

    private async void StartOnIdle(object sender, EventArgs args)
    {
        Application.Idle -= StartOnIdle;
        await StartServer();
    }

    private async void CheckServer(object sender, EventArgs args)
    {
        if (busy || checking) return;
        checking = true;
        try
        {
            string instance = await Task.Run(() => RunningInstance());
            if (busy || !tray.Visible) return;
            if (instance != null)
            {
                managed = instance == project;
                ShowStatus(managed ? "Сервер работает" : "Запущен другой экземпляр", green, true);
            }
            else if (managed || healthy)
                ShowStatus("Сервер недоступен", red, false);
        }
        finally { checking = false; }
    }

    private async Task StartServer()
    {
        if (busy) return;
        busy = true;
        ShowStatus("Проверка сервера", yellow, false);
        try
        {
            string instance = await Task.Run(() => RunningInstance());
            if (instance == project)
            {
                managed = true;
                busy = false;
                ShowStatus("Сервер работает", green, true);
                Open(Address);
                return;
            }
            if (instance != null && instance != project)
                throw new InvalidOperationException("На порту 8765 уже работает другой Research Agent. Остановите его перед запуском контейнера.");
            if (!File.Exists(Path.Combine(root, "compose.yaml")))
                throw new InvalidOperationException("Положите ResearchAgent.exe в папку проекта рядом с compose.yaml.");
            if (instance == null && await Task.Run(() => PortInUse()) && !await Task.Run(() => ContainerRunning()))
                throw new InvalidOperationException("Порт 8765 занят. Освободите его и повторите запуск из меню значка.");
            ShowStatus("Запуск Docker", yellow, false);
            await Task.Run(() => EnsureDocker());
            ShowStatus("Подготовка и запуск сервера", yellow, false);
            await Task.Run(() => {
                if (File.Exists(Path.Combine(root, "research-agent-image.tar.gz")))
                {
                    RunDocker("image load --input research-agent-image.tar.gz", 300, true);
                    RunDocker(Compose("up -d --no-build --pull never"), 90, true);
                }
                else RunDocker(Compose("up -d --build"), 900, true);
            });
            managed = true;
            for (int attempt = 0; attempt < 24; attempt++)
            {
                if (await Task.Run(() => RunningInstance()) == project)
                {
                    busy = false;
                    ShowStatus("Сервер работает", green, true);
                    Open(Address);
                    return;
                }
                await Task.Delay(1250);
            }
            throw new InvalidOperationException("Контейнер запущен, но сервер не ответил. Проверьте журнал контейнера в Docker Desktop.");
        }
        catch (Exception error) { ShowError(error); }
    }

    private async Task StopServer()
    {
        if (busy || !managed) return;
        busy = true;
        ShowStatus("Остановка сервера", yellow, false);
        try
        {
            await Task.Run(() => RunDocker(Compose("stop"), 60, true));
            managed = false;
            busy = false;
            ShowStatus("Остановлен", gray, false);
        }
        catch (Exception error) { ShowError(error); }
    }

    private string Compose(string command)
    {
        return "compose --project-name " + project + " --file compose.yaml " + command;
    }

    private bool ContainerRunning()
    {
        try { return RunDocker(Compose("ps --status running --quiet research-agent"), 5, true).Trim().Length > 0; }
        catch (Win32Exception) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    private void EnsureDocker()
    {
        try
        {
            if (RunDocker("info --format {{.OSType}}", 8, false).Trim() == "linux") return;
        }
        catch (Win32Exception) { throw new InvalidOperationException("Установите Docker Desktop, затем выберите «Запустить сервер»."); }
        if (File.Exists(dockerDesktop))
        {
            using (Process.Start(new ProcessStartInfo(dockerDesktop) { UseShellExecute = true, WindowStyle = ProcessWindowStyle.Hidden })) { }
        }
        var waiting = Stopwatch.StartNew();
        while (waiting.Elapsed < TimeSpan.FromMinutes(2))
        {
            string type = RunDocker("info --format {{.OSType}}", 8, false).Trim();
            if (type == "linux") return;
            if (type == "windows") throw new InvalidOperationException("Переключите Docker Desktop на Linux containers и повторите запуск.");
            Thread.Sleep(1500);
        }
        throw new InvalidOperationException("Docker не готов. Откройте Docker Desktop, завершите первоначальную настройку и повторите запуск.");
    }

    private string RunDocker(string arguments, int seconds, bool required)
    {
        // Use Docker Desktop's local Windows pipe even when the default context is remote.
        var info = new ProcessStartInfo(docker, "--host npipe:////./pipe/docker_engine " + arguments) {
            WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
        info.EnvironmentVariables.Remove("DOCKER_CONTEXT");
        info.EnvironmentVariables.Remove("DOCKER_HOST");
        info.EnvironmentVariables["RESEARCH_AGENT_INSTANCE"] = project;
        info.EnvironmentVariables["RESEARCH_AGENT_HOST_MATERIALS"] = Path.Combine(root, "Materials");
        using (var process = Process.Start(info))
        {
            var output = process.StandardOutput.ReadToEndAsync();
            var errors = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(seconds * 1000))
            {
                try { process.Kill(); } catch (InvalidOperationException) { }
                if (!required) return "";
                throw new InvalidOperationException("Docker не завершил команду вовремя. Повторите попытку из меню.");
            }
            Task.WaitAll(output, errors);
            if (process.ExitCode == 0) return output.Result;
            if (!required) return "";
            string message = errors.Result.Trim();
            string key = Environment.GetEnvironmentVariable("OPENAI_API_KEY");
            if (!String.IsNullOrEmpty(key)) message = message.Replace(key, "[скрыто]");
            throw new InvalidOperationException("Ошибка Docker.\n" + message.Substring(Math.Max(0, message.Length - 750)));
        }
    }

    private static string RunningInstance()
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(Address + "api/health");
            request.Proxy = null;
            request.Timeout = request.ReadWriteTimeout = 1200;
            using (var response = request.GetResponse())
            using (var stream = new StreamReader(response.GetResponseStream()))
            {
                var health = new JavaScriptSerializer().DeserializeObject(stream.ReadToEnd()) as System.Collections.Generic.Dictionary<string, object>;
                object app, worker, instance;
                if (health != null && health.TryGetValue("app", out app) && Equals(app, "research-agent") &&
                    health.TryGetValue("worker_alive", out worker) && Equals(worker, true))
                    return health.TryGetValue("instance", out instance) ? instance as string ?? "" : "";
            }
        }
        catch (WebException) { }
        catch (IOException) { }
        catch (ArgumentException) { }
        catch (InvalidOperationException) { }
        return null;
    }

    private static bool PortInUse()
    {
        using (var client = new TcpClient())
        {
            try { client.Connect(IPAddress.Loopback, 8765); return true; }
            catch (SocketException) { return false; }
        }
    }

    private void ShowStatus(string text, Icon icon, bool available)
    {
        healthy = available;
        status.Text = text;
        tray.Text = "Research Agent · " + text;
        tray.Icon = icon;
        start.Enabled = !busy && !available;
        stop.Enabled = !busy && managed;
        open.Enabled = available;
        exit.Enabled = !busy;
    }

    private void OpenMaterials()
    {
        string path = Path.Combine(root, "Materials");
        try { Directory.CreateDirectory(path); Open(path); }
        catch (IOException error) { Notify(error.Message); }
        catch (UnauthorizedAccessException error) { Notify(error.Message); }
    }

    private void Open(string target)
    {
        try { using (Process.Start(new ProcessStartInfo(target) { UseShellExecute = true })) { } }
        catch (Win32Exception error) { Notify(error.Message); }
    }

    private void ShowError(Exception error)
    {
        busy = false;
        ShowStatus("Требуется действие", red, false);
        Notify(error.Message);
    }

    private void Notify(string message)
    {
        tray.ShowBalloonTip(5000, "Research Agent", message, ToolTipIcon.Warning);
        MessageBox.Show(message, "Research Agent", MessageBoxButtons.OK, MessageBoxIcon.Warning);
    }

    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr icon);

    private static Icon StatusIcon(Color color)
    {
        using (var bitmap = new Bitmap(32, 32))
        using (var graphics = Graphics.FromImage(bitmap))
        using (var background = new SolidBrush(Color.FromArgb(27, 48, 45)))
        using (var indicator = new SolidBrush(color))
        using (var font = new Font("Segoe UI", 15, FontStyle.Bold, GraphicsUnit.Pixel))
        {
            graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            graphics.FillEllipse(background, 1, 1, 28, 28);
            graphics.DrawString("R", font, Brushes.White, 6, 5);
            graphics.FillEllipse(background, 18, 18, 14, 14);
            graphics.FillEllipse(indicator, 20, 20, 10, 10);
            IntPtr handle = bitmap.GetHicon();
            try { using (var icon = Icon.FromHandle(handle)) return (Icon)icon.Clone(); }
            finally { DestroyIcon(handle); }
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            Application.Idle -= StartOnIdle;
            timer.Dispose();
            tray.Visible = false;
            tray.Dispose();
            menu.Dispose();
            gray.Dispose(); yellow.Dispose(); green.Dispose(); red.Dispose();
        }
        base.Dispose(disposing);
    }
}
