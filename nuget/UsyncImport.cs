using Microsoft.Extensions.DependencyInjection;
using Umbraco.Cms.Core.Web;
using uSync.BackOffice;
using uSync.BackOffice.Configuration;
using uSync.BackOffice.SyncHandlers.Models;
using uSync.Core;

namespace VNS.Harbor;

/// <summary>What an import did: how many items it changed, and those it could not import, by name and why</summary>
public sealed record ImportResult(int Changed, int Failed, IReadOnlyList<string> Problems);

/// <summary>
/// uSync's own import of one group (Settings, or Content, which takes content, media and dictionary), from the folders
/// uSync is set up with, as its Import button does: only what differs is imported. The types of uSync are used only
/// here, so a site without uSync loads none of them (Installed tells first).
/// </summary>
public static class UsyncImport {
	public static bool Installed => AppDomain.CurrentDomain.GetAssemblies().Any(assembly => assembly.GetName().Name == "uSync.BackOffice");

	/// <summary>Run after the request that started it has ended: its own scope, from the application's scope factory, as
	/// the request's services are disposed with it</summary>
	public static async Task<ImportResult> RunAsync(IServiceScopeFactory scopes, string group) {
		using var scope = scopes.CreateScope();
		var provider = scope.ServiceProvider;
		// The import saves through Umbraco's services, which want an Umbraco context on this thread
		using var context = provider.GetRequiredService<IUmbracoContextFactory>().EnsureUmbracoContext();
		var folders = provider.GetRequiredService<ISyncConfigService>().GetFolders();
		var actions = (await provider.GetRequiredService<ISyncService>().StartupImportAsync(folders, false, new SyncHandlerOptions(group), null)).ToList();
		var failed = actions.Where(action => !action.Success).ToList();
		var changed = actions.Count(action => action.Success && action.Change > ChangeType.NoChange);
		var problems = failed.Take(20).Select(action => $"{action.Name}: {action.Message ?? action.Exception?.Message ?? "not imported"}").ToList();
		return new ImportResult(changed, failed.Count, problems);
	}
}
