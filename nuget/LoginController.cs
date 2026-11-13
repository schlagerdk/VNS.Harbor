using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Caching.Memory;
using Microsoft.Extensions.Logging;
using Umbraco.Cms.Core.Security;
using Umbraco.Cms.Web.Common.Security;

namespace VNS.Harbor;

/// <summary>
/// The Harbor admin's Backend button: a one-time link that signs the Umbraco user chosen for this site in, with
/// Umbraco's own sign-in, and opens the backoffice. Any problem gives the backoffice's own login, never the reason.
/// </summary>
[AllowAnonymous]
public sealed class LoginController(
	IWebHostEnvironment host,
	IBackOfficeUserManager users,
	IBackOfficeSignInManager signIn,
	IMemoryCache used,
	ILogger<LoginController> logger): Controller {
	[HttpGet("/umbraco/vns-harbor/login")]
	public async Task<IActionResult> Login(string? token, [FromQuery(Name = "return")] string? returnPath) {
		// Only somewhere in the backoffice
		var target = returnPath is not null && returnPath.StartsWith("/umbraco/", StringComparison.Ordinal) && !returnPath.Contains("//") ? returnPath : "/umbraco";
		var connection = HarborConnection.Read(host);
		var login = connection is null || string.IsNullOrEmpty(token) ? null : LoginToken.Verify(token, connection.LoginKey, DateTimeOffset.UtcNow);
		if (connection is null || login is null || login.Site != connection.Site || login.Environment != connection.Environment) {
			logger.LogWarning("VNS Harbor: a backoffice login link was refused");
			return Redirect("/umbraco");
		}
		// A link works once
		if (used.TryGetValue("vns-harbor-login:" + login.Nonce, out _)) {
			logger.LogWarning("VNS Harbor: a backoffice login link was used again");
			return Redirect("/umbraco");
		}
		used.Set("vns-harbor-login:" + login.Nonce, true, TimeSpan.FromMinutes(10));
		var user = await users.FindByEmailAsync(login.User) ?? await users.FindByNameAsync(login.User);
		if (user is null || !user.IsApproved || user.IsLockedOut) {
			logger.LogWarning("VNS Harbor: the Umbraco user {User} for backoffice logins was not found or may not log in", login.User);
			return Redirect("/umbraco");
		}
		await signIn.SignInAsync(user, isPersistent: false);
		logger.LogInformation("VNS Harbor: {User} logged in to the backoffice from Harbor", login.User);
		return Redirect(target);
	}
}