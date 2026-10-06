using System;
using System.IO;
using System.Runtime.InteropServices;

namespace GPUqueueDesktop
{
    internal sealed class CpuSampler
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
        private long previousIdle, previousTotal;
        private bool primed;

        internal static double? Calculate(long idleDelta, long totalDelta)
        {
            if (totalDelta <= 0 || idleDelta < 0 || idleDelta > totalDelta) return null;
            return 100.0 * (totalDelta - idleDelta) / totalDelta;
        }
        internal double? Sample()
        {
            long idle, kernel, user;
            if (!GetSystemTimes(out idle, out kernel, out user)) { primed = false; return null; }
            long total = kernel + user;
            double? result = primed ? Calculate(idle - previousIdle, total - previousTotal) : null;
            previousIdle = idle; previousTotal = total; primed = true;
            return result;
        }
    }

    internal static class TrayAutostart
    {
        // Only the installer's existing, exact tray task can be enabled/disabled.
        // No task creation, commands, credentials, registry or broker changes.
        internal const string TaskName = "GPUqueue Tray";
        internal static bool GetEnabled() { return Access(null); }
        internal static bool SetEnabled(bool enabled) { return Access(enabled); }
        private static bool Access(bool? change)
        {
            dynamic service = null, folder = null, task = null, definition = null, actions = null, action = null;
            try
            {
                service = Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service", true));
                service.Connect(); folder = service.GetFolder("\\"); task = folder.GetTask(TaskName);
                definition = task.Definition; actions = definition.Actions;
                if (actions.Count != 1) throw new InvalidOperationException("Unerwartete Autostart-Aufgabe.");
                action = actions.Item(1);
                string expected = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "GPUqueue", "GPUqueue.exe");
                if ((int)action.Type != 0 || !String.Equals(Path.GetFullPath((string)action.Path), expected, StringComparison.OrdinalIgnoreCase) || (string)action.Arguments != "--tray")
                    throw new InvalidOperationException("Autostart-Aufgabe gehört nicht zu dieser Installation.");
                if (change.HasValue) task.Enabled = change.Value;
                return (bool)task.Enabled;
            }
            finally
            {
                Release(action); Release(actions); Release(definition); Release(task); Release(folder); Release(service);
            }
        }
        private static void Release(object value) { if (value != null && Marshal.IsComObject(value)) Marshal.FinalReleaseComObject(value); }
    }
}
