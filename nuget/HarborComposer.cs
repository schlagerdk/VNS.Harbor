using Microsoft.Extensions.DependencyInjection;
using Umbraco.Cms.Core.Composing;
using Umbraco.Cms.Core.DependencyInjection;
using Umbraco.Cms.Core.Events;
using Umbraco.Cms.Core.Models.Membership;
using Umbraco.Cms.Core.Notifications;
using Umbraco.Cms.Core.Services;
using Umbraco.Cms.Core.Strings;

namespace VNS.Harbor;

public sealed class HarborComposer: IComposer {
	public void Compose(IUmbracoBuilder builder) {
		builder.Services.AddMemoryCache();
		builder.Services.AddHttpClient("VNS.Harbor");
		builder.AddNotificationAsyncHandler<UmbracoApplicationStartedNotification, HarborGroup>();
	}
}

/// <summary>
/// The user group whose members may use the Harbor Sync dashboard. It is made empty: nobody may until an administrator
/// adds them.
/// </summary>
public sealed class HarborGroup(IUserGroupService groups, IShortStringHelper strings): INotificationAsyncHandler<UmbracoApplicationStartedNotification> {
	public const string Alias = "vnsHarbor";
	public const string Name = "Harbor Sync";

	public async Task HandleAsync(UmbracoApplicationStartedNotification notification, CancellationToken cancellationToken) {
		var found = await groups.GetAsync(Alias);
		if (found is not null) {
			// Made as VNS Harbor Sync before 18.3.3: renamed once, a name an administrator gave it is kept
			if (found.Name == "VNS Harbor Sync") {
				found.Name = Name;
				await groups.UpdateAsync(found, Umbraco.Cms.Core.Constants.Security.SuperUserKey);
			}
			return;
		}
		var group = new UserGroup(strings, 0, Alias, Name, "icon-sync");
		await groups.CreateAsync(group, Umbraco.Cms.Core.Constants.Security.SuperUserKey);
	}
}