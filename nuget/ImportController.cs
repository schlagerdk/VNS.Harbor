using System.Collections.Concurrent;
using System.Security.Cryptography;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Caching.Memory;
using Microsoft.Extensions.Logging;

namespace VNS.Harbor;

/// <summary>
/// After Harbor has synced settings or data here: uSync imports them now, while the site runs, so it needs no restart.
/// Harbor starts it with a one-time command signed with the site's login key, and follows it by the id it gets back,
/// as a large import of content takes a while. One import at a time.
/// </summary>
[AllowAnonymous]
public sealed class ImportController(
	IWebHostEnvironment host,
	IServiceScopeFactory scopes,
	IMemoryCache used,
	ILogger<ImportController> logger): Controller {
	private static readonly SemaphoreSlim One = new(1, 1);
	private static readonly ConcurrentDictionary<string, ImportState> Imports = new();
	private static readonly string[] Groups = ["Settings", "Content"];

	public sealed record ImportState(string Group, DateTimeOffset Started) {
		public string State { get; set; } = "running";
		public ImportResult? Result { get; set; }
		public string Error { get; set; } = "";
		public DateTimeOffset? Ended { get; set; }
	}

	[HttpPost("/umbraco/vns-harbor/import")]
	public IActionResult Start() {
		var connection = HarborConnection.Read(host);
		var token = Request.Headers["X-Harbor-Command"].ToString();
		var command = connection is null || string.IsNullOrEmpty(token) ? null : HarborCommand.Verify(token, connection.LoginKey, DateTimeOffset.UtcNow);
		if (connection is null || command is null || command.Action != "import" || command.Site != connection.Site || command.Environment != connection.Environment
			|| !Groups.Contains(command.Group)) {
			logger.LogWarning("VNS Harbor: an import was refused");
			return Unauthorized();
		}
		// A command works once
		if (used.TryGetValue("vns-harbor-command:" + command.Nonce, out _)) {
			return Unauthorized();
		}
		used.Set("vns-harbor-command:" + command.Nonce, true, TimeSpan.FromMinutes(10));
		if (!UsyncImport.Installed) {
			return Conflict(new { error = "uSync is not installed on this site" });
		}
		if (!One.Wait(0)) {
			return Conflict(new { error = "An import runs already" });
		}
		var id = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
		var state = new ImportState(command.Group, DateTimeOffset.UtcNow);
		Imports[id] = state;
		foreach (var old in Imports.Where(item => item.Value.Ended < DateTimeOffset.UtcNow.AddHours(-1)).Select(item => item.Key).ToList()) {
			Imports.TryRemove(old, out _);
		}
		_ = Task.Run(async () => {
			try {
				// The request's own services are gone once it has answered: the import has a scope of its own
				state.Result = await UsyncImport.RunAsync(scopes, command.Group);
				state.State = "done";
				logger.LogInformation("VNS Harbor: uSync imported {Group}: {Changed} changed, {Failed} failed", command.Group, state.Result.Changed, state.Result.Failed);
			} catch (Exception exception) {
				state.State = "failed";
				state.Error = exception.Message;
				logger.LogError(exception, "VNS Harbor: uSync could not import {Group}", command.Group);
			} finally {
				state.Ended = DateTimeOffset.UtcNow;
				One.Release();
			}
		});
		return Accepted(new { id });
	}

	/// <summary>How an import is going: its id is the secret, unguessable and known only to the Harbor that started it</summary>
	[HttpGet("/umbraco/vns-harbor/import/{id}")]
	public IActionResult Status(string id) {
		if (!Imports.TryGetValue(id, out var state)) {
			return NotFound();
		}
		return Ok(new {
			state = state.State, group = state.Group, changed = state.Result?.Changed ?? 0, failed = state.Result?.Failed ?? 0,
			problems = state.Result?.Problems ?? [], error = state.Error,
			seconds = Math.Round(((state.Ended ?? DateTimeOffset.UtcNow) - state.Started).TotalSeconds, 1),
		});
	}
}
