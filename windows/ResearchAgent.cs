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
    private enum ServerMode { None, Local, Container, Other }

    private const string Address = "http://127.0.0.1:8765/";
    private readonly string root;
    private readonly string project;
    private readonly string docker;
    private readonly string dockerDesktop;
    private readonly NotifyIcon tray;
    private readonly ContextMenuStrip menu;
    private readonly ToolStripMenuItem status, start, stop, open, restartContainer, restartLocal, errorDetails, exit;
    private readonly System.Windows.Forms.Timer timer;
    private readonly Icon gray = StatusIcon(Color.FromArgb(138, 146, 151));
    private readonly Icon yellow = StatusIcon(Color.FromArgb(252, 193, 72));
    private readonly Icon green = StatusIcon(Color.FromArgb(78, 218, 142));
    private readonly Icon red = StatusIcon(Color.FromArgb(243, 126, 91));
    private bool busy, checking;
    private int operation;
    private ServerMode mode, preferred = ServerMode.Local;
    private Process localProcess;
    private Task<string> localErrors;
    private string lastError;

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
        start = new ToolStripMenuItem("Запустить сервер", null, async (s, e) => await StartServer(preferred));
        stop = new ToolStripMenuItem("Остановить сервер", null, async (s, e) => await StopServer(false));
        restartContainer = new ToolStripMenuItem("Перезапустить в контейнере", null, async (s, e) => await StartServer(ServerMode.Container));
        restartLocal = new ToolStripMenuItem("Перезапустить локально", null, async (s, e) => await StartServer(ServerMode.Local));
        errorDetails = new ToolStripMenuItem("Сведения об ошибке", null,
            (s, e) => MessageBox.Show(lastError, "Research Agent", MessageBoxButtons.OK, MessageBoxIcon.Warning));
        errorDetails.Visible = false;
        exit = new ToolStripMenuItem("Выйти", null, async (s, e) => await StopServer(true));
        menu.Items.AddRange(new ToolStripItem[] { status, new ToolStripSeparator(), open, start, stop,
            restartContainer, restartLocal, new ToolStripMenuItem("Открыть папку Materials", null, (s, e) => OpenMaterials()),
            errorDetails, new ToolStripSeparator(), exit });
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
        await StartServer(ServerMode.Local, reuseRunning: true);
    }

    private async void CheckServer(object sender, EventArgs args)
    {
        if (busy || checking) return;
        checking = true;
        int version = operation;
        try
        {
            string instance = await Task.Run(() => RunningInstance());
            if (busy || version != operation || !tray.Visible) return;
            if (instance != null)
            {
                ShowRunning(ModeOf(instance));
            }
            else if (mode != ServerMode.None)
            {
                if (mode == ServerMode.Other || mode == ServerMode.Local && !LocalRunning())
                    mode = ServerMode.None;
                ShowStatus("Сервер недоступен", red, false);
            }
        }
        finally { checking = false; }
    }

    private ServerMode ModeOf(string instance)
    {
        if (instance == project + "-local") return ServerMode.Local;
        if (instance == project) return ServerMode.Container;
        return instance == null ? ServerMode.None : ServerMode.Other;
    }

    private void ShowRunning(ServerMode running)
    {
        mode = running;
        if (running == ServerMode.Other)
            ShowStatus("Запущен другой экземпляр", green, true);
        else
        {
            preferred = running;
            ShowStatus(running == ServerMode.Local ? "Сервер работает локально" : "Сервер работает в контейнере", green, true);
        }
    }

    private async Task StartServer(ServerMode target, bool reuseRunning = false)
    {
        if (busy) return;
        operation++;
        errorDetails.Available = false;
        busy = true;
        ShowStatus("Проверка сервера", yellow, false);
        ServerMode previous = ServerMode.None;
        bool starting = false;
        string message;
        try
        {
            var running = ModeOf(await Task.Run(() => RunningInstance()));
            if (running != ServerMode.None) mode = running;
            if (running != ServerMode.None && (reuseRunning || running == target))
            {
                busy = false;
                ShowRunning(running);
                Open(Address);
                return;
            }
            if (running == ServerMode.Other || running == ServerMode.None && mode == ServerMode.None &&
                await Task.Run(() => PortInUse()))
                throw new InvalidOperationException("Порт 8765 занят другим приложением. Остановите его перед запуском Research Agent.");

            if (target == ServerMode.Container)
            {
                if (!File.Exists(Path.Combine(root, "compose.yaml")))
                    throw new InvalidOperationException("Распакуйте весь архив Research Agent рядом с ResearchAgent.exe.");
                ShowStatus("Подготовка Docker", yellow, false);
                await Task.Run(() => {
                    EnsureDocker();
                    RunDocker(Compose("build"), 900, true);
                });
            }
            else if (!File.Exists(LocalExecutable))
                throw new InvalidOperationException("Сервер не найден. Распакуйте весь архив Research Agent.");

            if (mode == ServerMode.Local || mode == ServerMode.Container)
            {
                previous = mode;
                ShowStatus("Остановка сервера", yellow, false);
                await StopActive();
            }
            starting = true;
            await Launch(target);
            busy = false;
            ShowRunning(target);
            if (reuseRunning || previous == ServerMode.None) Open(Address);
            return;
        }
        catch (Exception error) { message = error.Message; }

        if (starting)
        {
            try
            {
                await StopActive();
                if (previous == ServerMode.Local || previous == ServerMode.Container)
                {
                    await Launch(previous);
                    message += "\nПредыдущий режим восстановлен.";
                }
            }
            catch (Exception recovery) { message += "\n" + recovery.Message; }
        }
        var available = ModeOf(await Task.Run(() => RunningInstance()));
        busy = false;
        if (available != ServerMode.None) ShowRunning(available);
        else ShowStatus("Требуется действие", red, false);
        Notify(message);
    }

    private string LocalExecutable { get { return Path.Combine(root, "runtime", "research-server", "research-server.exe"); } }

    private bool LocalRunning() { return localProcess != null && !localProcess.HasExited; }

    private void StartLocal()
    {
        var info = new ProcessStartInfo(LocalExecutable, "--stop-on-stdin --data-dir data --materials-dir Materials") {
            WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
        info.EnvironmentVariables["RESEARCH_AGENT_INSTANCE"] = project + "-local";
        info.EnvironmentVariables.Remove("RESEARCH_AGENT_HOST_MATERIALS");
        info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        if (localProcess != null) localProcess.Dispose();
        localProcess = Process.Start(info);
        localProcess.BeginOutputReadLine();
        localErrors = localProcess.StandardError.ReadToEndAsync();
    }

    private async Task Launch(ServerMode target)
    {
        if (await Task.Run(() => PortInUse()))
            throw new InvalidOperationException("Порт 8765 не освободился. Повторите запуск после остановки другого сервера.");
        mode = target;
        ShowStatus("Запуск сервера", yellow, false);
        if (target == ServerMode.Local) StartLocal();
        else await Task.Run(() => RunDocker(Compose("up -d --no-build --pull never"), 90, true));

        for (int attempt = 0; attempt < 30; attempt++)
        {
            if (ModeOf(await Task.Run(() => RunningInstance())) == target) return;
            if (target == ServerMode.Local && !LocalRunning())
                throw new InvalidOperationException("Локальный сервер завершился при запуске.\n" + ErrorTail(await localErrors));
            await Task.Delay(1000);
        }
        throw new InvalidOperationException("Сервер не ответил после запуска.");
    }

    private async Task StopActive()
    {
        if (mode == ServerMode.Local && LocalRunning())
        {
            localProcess.StandardInput.Close();
            if (!await Task.Run(() => localProcess.WaitForExit(45000)))
                throw new InvalidOperationException("Локальный сервер ещё завершает работу. Повторите попытку позже.");
        }
        if (mode == ServerMode.Container)
            await Task.Run(() => RunDocker(Compose("stop"), 60, true));
        mode = ServerMode.None;
    }

    private async Task StopServer(bool leave)
    {
        if (busy) return;
        operation++;
        errorDetails.Available = false;
        busy = true;
        ShowStatus("Остановка сервера", yellow, false);
        try
        {
            await StopActive();
            busy = false;
            if (leave) { ExitThread(); return; }
            ShowStatus("Остановлен", gray, false);
        }
        catch (Exception error) { ShowError(error); }
    }

    private string Compose(string command)
    {
        return "compose --project-name " + project + " --file compose.yaml " + command;
    }

    private void EnsureDocker()
    {
        try
        {
            string type = RunDocker("info --format {{.OSType}}", 8, false).Trim();
            if (type == "linux") return;
            if (type == "windows") throw new InvalidOperationException("Переключите Docker Desktop на Linux containers и повторите запуск.");
        }
        catch (Win32Exception) { throw new InvalidOperationException("Для запуска в контейнере установите Docker Desktop."); }
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
        info.EnvironmentVariables["RESEARCH_AGENT_IMAGE"] = project + ":local";
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
            throw new InvalidOperationException("Ошибка Docker.\n" + ErrorTail(errors.Result));
        }
    }

    private static string ErrorTail(string message)
    {
        string key = Environment.GetEnvironmentVariable("OPENAI_API_KEY");
        if (!String.IsNullOrEmpty(key)) message = message.Replace(key, "[скрыто]");
        message = message.Trim();
        return message.Substring(Math.Max(0, message.Length - 750));
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
        status.Text = text;
        tray.Text = "Research Agent · " + text;
        tray.Icon = icon;
        start.Enabled = !busy && mode == ServerMode.None;
        stop.Enabled = !busy && (mode == ServerMode.Local || mode == ServerMode.Container);
        restartContainer.Enabled = !busy && mode != ServerMode.Other && mode != ServerMode.Container;
        restartLocal.Enabled = !busy && mode != ServerMode.Other && mode != ServerMode.Local;
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
        lastError = message;
        errorDetails.Available = true;
        tray.ShowBalloonTip(5000, "Research Agent", message, ToolTipIcon.Warning);
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
            if (localProcess != null) localProcess.Dispose();
            gray.Dispose(); yellow.Dispose(); green.Dispose(); red.Dispose();
        }
        base.Dispose(disposing);
    }
}
