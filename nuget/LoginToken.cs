using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace VNS.Harbor;

/// <summary>
/// A one-time backoffice login made by Harbor: base64url(JSON) "." base64url(HMAC-SHA256 of the first part, keyed with
/// the site's login key). The JSON names the site, the environment, the Umbraco user, when it expires and a nonce.
/// </summary>
public sealed record LoginToken(string Site, string Environment, string User, long Expires, string Nonce) {
	public static LoginToken? Verify(string token, string key, DateTimeOffset now) {
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
			var login = new LoginToken(root.GetProperty("site").GetString() ?? "", root.GetProperty("environment").GetString() ?? "",
				root.GetProperty("user").GetString() ?? "", root.GetProperty("exp").GetInt64(), root.GetProperty("nonce").GetString() ?? "");
			return login.Expires >= now.ToUnixTimeSeconds() && login.Nonce.Length >= 16 ? login : null;
		} catch (Exception exception) when (exception is JsonException or KeyNotFoundException or InvalidOperationException) {
			return null;
		}
	}

	private static byte[] FromBase64Url(string value) {
		var text = value.Replace('-', '+').Replace('_', '/');
		return Convert.FromBase64String(text.PadRight(text.Length + (4 - text.Length % 4) % 4, '='));
	}
}