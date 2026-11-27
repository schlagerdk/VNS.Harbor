using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace VNS.Harbor;

/// <summary>
/// A one-time command from Harbor, signed as a backoffice login is: base64url(JSON) "." base64url(HMAC-SHA256 of the
/// first part, keyed with the site's login key). The JSON names the site, the environment, what to do (such as
/// "import"), its group (Settings or Content), when it expires and a nonce.
/// </summary>
public sealed record HarborCommand(string Site, string Environment, string Action, string Group, long Expires, string Nonce) {
	public static HarborCommand? Verify(string token, string key, DateTimeOffset now) {
		var parts = token.Split('.');
		if (parts.Length != 2 || string.IsNullOrEmpty(key)) {
			return null;
		}
		byte[] payload, signature;
		try {
			payload = FromBase64Url(parts[0]);
			signature = FromBase64Url(parts[1]);
		} catch (FormatException) {
			return null;
		}
		var expected = HMACSHA256.HashData(Encoding.UTF8.GetBytes(key), Encoding.ASCII.GetBytes(parts[0]));
		if (!CryptographicOperations.FixedTimeEquals(expected, signature)) {
			return null;
		}
		try {
			using var document = JsonDocument.Parse(payload);
			var root = document.RootElement;
			// A login token has no action: it is never taken for a command
			if (!root.TryGetProperty("action", out var action)) {
				return null;
			}
			var command = new HarborCommand(root.GetProperty("site").GetString() ?? "", root.GetProperty("environment").GetString() ?? "",
				action.GetString() ?? "", root.TryGetProperty("group", out var group) ? group.GetString() ?? "" : "",
				root.GetProperty("exp").GetInt64(), root.GetProperty("nonce").GetString() ?? "");
			return command.Expires >= now.ToUnixTimeSeconds() && command.Nonce.Length >= 16 ? command : null;
		} catch (Exception exception) when (exception is JsonException or KeyNotFoundException or InvalidOperationException) {
			return null;
		}
	}

	private static byte[] FromBase64Url(string value) {
		var text = value.Replace('-', '+').Replace('_', '/');
		return Convert.FromBase64String(text.PadRight(text.Length + (4 - text.Length % 4) % 4, '='));
	}
}
