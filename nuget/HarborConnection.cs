using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Hosting;

namespace VNS.Harbor;

/// <summary>
/// The site's connection to the Harbor that manages it: vns-harbor.json in the folder the site runs from, written by
/// Harbor when it publishes the site (never in appsettings or the repository). Read again when it changes.
/// </summary>
public sealed class HarborConnection {
	public const string FileName = "vns-harbor.json";

	[JsonPropertyName("harbor")] public string Harbor { get; init; } = "";
	[JsonPropertyName("site")] public string Site { get; init; } = "";
	[JsonPropertyName("environment")] public string Environment { get; init; } = "";
	[JsonPropertyName("token")] public string Token { get; init; } = "";
	[JsonPropertyName("loginKey")] public string LoginKey { get; init; } = "";

	public static HarborConnection? Read(IWebHostEnvironment host) {
		var path = Path.Combine(host.ContentRootPath, FileName);
		try {
			return File.Exists(path) ? JsonSerializer.Deserialize<HarborConnection>(File.ReadAllText(path)) : null;
		} catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException) {
			return null;
		}
	}
}
