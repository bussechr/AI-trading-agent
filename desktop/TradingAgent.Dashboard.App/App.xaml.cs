using System.Net.Http;
using System.Windows;
using Serilog;
using TradingAgent.Dashboard.App.ViewModels;
using TradingAgent.Dashboard.Core.Services;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Services;

namespace TradingAgent.Dashboard.App;

public partial class App : Application
{
    private DashboardShellViewModel? _shell;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        ConfigureLogging();

        var options = DashboardOptionsLoader.Load();
        Log.Information("Dashboard starting with Bridge={BridgeBaseUrl}, RuntimeDb={RuntimeDbPath}", options.BridgeBaseUrl, options.RuntimeDbPath);

        var bridgeClient = new BridgeTelemetryClient(new HttpClient(), options);
        var dbReader = new RuntimeDbReader(options);
        var aggregator = new TelemetryAggregator();
        var ingestion = new TelemetryIngestionService(bridgeClient, dbReader, aggregator, options);

        _shell = new DashboardShellViewModel(ingestion, options);

        var window = new MainWindow
        {
            DataContext = _shell,
        };

        MainWindow = window;
        window.Show();

        await _shell.StartAsync();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            _shell?.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
        catch (Exception exc)
        {
            Log.Warning(exc, "Dashboard shutdown had non-fatal cleanup error");
        }
        finally
        {
            Log.CloseAndFlush();
        }

        base.OnExit(e);
    }

    private static void ConfigureLogging()
    {
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TradingAgent.Dashboard",
            "logs"
        );
        Directory.CreateDirectory(dir);

        Log.Logger = new LoggerConfiguration()
            .MinimumLevel.Information()
            .WriteTo.File(
                Path.Combine(dir, "dashboard-.log"),
                rollingInterval: RollingInterval.Day,
                retainedFileCountLimit: 7,
                shared: true
            )
            .CreateLogger();
    }
}
