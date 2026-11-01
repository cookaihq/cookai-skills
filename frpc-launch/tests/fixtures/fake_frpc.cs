// Network-free executable fixture for Windows lifecycle tests, not an FRP client.
using System;
using System.IO;
using System.Diagnostics;
using System.Text.RegularExpressions;
using System.Threading;

class FakeFrpc {
    static int Main(string[] args) {
        if (Array.IndexOf(args, "--version") >= 0) { Console.WriteLine("synthetic-1.0"); return 0; }
        if (Array.IndexOf(args, "--child") >= 0) { Thread.Sleep(90000); return 0; }
        string config = Environment.GetEnvironmentVariable("FRPC_LAUNCH_RUNTIME_CONFIG") ?? "";
        int ci = Array.IndexOf(args, "-c");
        if (config == "" && ci >= 0) config = File.ReadAllText(args[ci + 1]);
        if (Array.IndexOf(args, "verify") >= 0) {
            if (config.Contains("invalid-verify.example.test")) { Console.WriteLine("invalid synthetic configuration"); return 1; }
            Console.WriteLine("syntax is ok"); return 0;
        }
        Console.WriteLine("[fixture] client_pid=" + Process.GetCurrentProcess().Id);
        Console.WriteLine("[fixture] console=" + GetConsoleWindow());
        string token = Environment.GetEnvironmentVariable("NATFRP_TOKEN");
        if (token == null) token = Regex.Match(config, @"(?:auth\.)?token\s*=\s*""([^""]*)""").Groups[1].Value;
        Console.WriteLine("token=" + token);
        if (config.Contains("failure.example.test")) Console.WriteLine("connect to server error: synthetic rejection");
        else if (!config.Contains("silent.example.test")) {
            if (config.Contains("delayed.example.test")) Thread.Sleep(2000);
            Console.WriteLine("login to server success");
            Console.WriteLine("start proxy success");
        }
        if (config.Contains("descendant.example.test")) {
            Process child = Process.Start(new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, "--child") {
                UseShellExecute = false, CreateNoWindow = true });
            Console.WriteLine("[fixture] descendant_pid=" + child.Id);
        }
        if (config.Contains("exit.example.test")) return 7;
        Thread.Sleep(90000);
        return 0;
    }
    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    static extern IntPtr GetConsoleWindow();
}
