using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Umbraco.Cms.Api.Management.Controllers;
using Umbraco.Cms.Api.Management.Routing;
using Umbraco.Cms.Core.Security;

namespace VNS.Harbor;

/// <summary>
/// The dashboard's way to Harbor: what this site may sync to, starting a sync, and its steps. The site asks Harbor with
/// its own token (vns-harbor.json); only members of the VNS Harbor user group may.
/// </summary>
[VersionedApiBackOfficeRoute("vns-harbor")]
[ApiExplorerSettings(GroupName = "VNS Harbor")]
public sealed class HarborApiController(IWebHostEnvironment host, IBackOfficeSecurityAccessor security, IHttpClientFactory clients): ManagementApiControllerBase {
	private bool Allowed => security.BackOfficeSecurity?.CurrentUser?.Groups.Any(group => group.Alias == HarborGroup.Alias) == true;

	[HttpGet("status")]
	public async Task<IActionResult> Status() {
		var connection = HarborConnection.Read(host);
		if (!Allowed) {
			return Ok(new { allowed = false, connected = connection is not null });
		}
		if (connection is null || string.IsNullOrEmpty(connection.Harbor)) {
			return Ok(new { allowed = true, connected = false });
		}
		var (status, body) = await Ask(connection, HttpMethod.Get, "site", null);
		return Ok(new { allowed = true, connected = true, harbor = connection.Harbor, reachable = status == 200, site = status == 200 ? body : null });
	}

	public sealed record SyncRequest(string Destination, string What);

	[HttpPost("sync")]
	public async Task<IActionResult> Sync([FromBody] SyncRequest request) {
		var connection = HarborConnection.Read(host);
		if (!Allowed || connection is null) {
			return Forbid();
		}
		var (status, body) = await Ask(connection, HttpMethod.Post, "sync", new { destination = request.Destination, what = request.What });
		return StatusCode(status == 0 ? StatusCodes.Status502BadGateway : status, body);
	}

	[HttpGet("task/{id}")]
	public async Task<IActionResult> Task(string id) {
		var connection = HarborConnection.Read(host);
		if (!Allowed || connection is null || !System.Text.RegularExpressions.Regex.IsMatch(id, "^[0-9a-f]{12}$")) {
			return Forbid();
		}
		var (status, body) = await Ask(connection, HttpMethod.Get, "task/" + id, null);
		return StatusCode(status == 0 ? StatusCodes.Status502BadGateway : status, body);
	}

	/// <summary>Harbor's answer as JSON, or status 0 when it could not be reached</summary>
	private async Task<(int Status, JsonElement? Body)> Ask(HarborConnection connection, HttpMethod method, string path, object? content) {
		using var message = new HttpRequestMessage(method, connection.Harbor.TrimEnd('/') + "/api/harbor/v1/" + path);
		message.Headers.Authorization = new AuthenticationHeaderValue("Bearer", connection.Token);
		if (content is not null) {
			message.Content = new StringContent(JsonSerializer.Serialize(content), Encoding.UTF8, "application/json");
		}
		try {
			using var client = clients.CreateClient("VNS.Harbor");
			client.Timeout = TimeSpan.FromSeconds(15);
			using var response = await client.SendAsync(message);
			var text = await response.Content.ReadAsStringAsync();
			JsonElement? body = null;
			try {
				body = JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text).RootElement.Clone();
			} catch (JsonException) {
			}
			return ((int)response.StatusCode, body);
		} catch (Exception exception) when (exception is HttpRequestException or TaskCanceledException) {
			return (0, null);
		}
	}
}