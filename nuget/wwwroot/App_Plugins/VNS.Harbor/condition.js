// The VNS Harbor Sync tab is only shown when the site takes part in a sync: Harbor is asked, as the dashboard asks it,
// and the tab stays away when Sync setup lets this site neither send to nor receive from any environment. A site that
// only receives keeps it: it shows when it last received and what has not reached it yet. A site Harbor does not answer
// for (not connected, out of reach, or a user without access) keeps the tab, so the dashboard can tell why.
import { UmbConditionBase } from "@umbraco-cms/backoffice/extension-registry";
import { UMB_AUTH_CONTEXT } from "@umbraco-cms/backoffice/auth";

const API = "/umbraco/management/api/v1/vns-harbor/";

export class VnsHarborHasDestinationsCondition extends UmbConditionBase {
	constructor(host, args) {
		super(host, args);
		this.consumeContext(UMB_AUTH_CONTEXT, async (auth) => {
			if (!auth) return;
			try {
				const token = await auth.getLatestToken();
				const response = await fetch(API + "status", { headers: { Authorization: "Bearer " + token } });
				const state = response.ok ? await response.json() : null;
				const site = state?.site;
				const nothing = state?.allowed && state.connected && state.reachable && !(site?.destinations || []).length && !(site?.sources || []).length;
				this.permitted = !nothing;
			} catch {
				this.permitted = true;
			}
		});
	}
}

export { VnsHarborHasDestinationsCondition as api };
