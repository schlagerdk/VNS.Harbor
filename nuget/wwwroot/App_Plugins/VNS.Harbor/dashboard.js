// VNS Harbor in the backoffice: sync this site's settings or data to its environments, through the Harbor that manages
// it. The site asks Harbor itself (VNS.Harbor's API, with the site's token); Sync setup in Harbor decides what may go.
import { LitElement, html, css, nothing } from "@umbraco-cms/backoffice/external/lit";
import { UmbElementMixin } from "@umbraco-cms/backoffice/element-api";
import { UMB_AUTH_CONTEXT } from "@umbraco-cms/backoffice/auth";

const API = "/umbraco/management/api/v1/vns-harbor/";
const COLORS = { development: "#6ca6f8", test: "#d6a54a", staging: "#a97cf2", production: "#e5574d", main: "#3fb950" };
// Each environment's own icon, in its colour
const ICONS = { development: "icon-lab", test: "icon-checkbox", staging: "icon-server-alt", production: "icon-globe", main: "icon-home" };
// A sync that went well closes by itself after this long, as Harbor's tasks do
const CLOSE_AFTER = 6000;

export default class VnsHarborDashboard extends UmbElementMixin(LitElement) {
	static properties = { _state: { state: true }, _task: { state: true }, _busy: { state: true }, _forced: { state: true }, _closing: { state: true } };

	constructor() {
		super();
		this._state = null;
		this._task = null;
		this._busy = false;
		// The environments Sync anyway was chosen for: their buttons are on although nothing has changed here
		this._forced = new Set();
		this.consumeContext(UMB_AUTH_CONTEXT, (auth) => {
			this._auth = auth;
			this.#load();
		});
	}

	async #call(path, options = {}) {
		const token = await this._auth.getLatestToken();
		const response = await fetch(API + path, {
			...options,
			headers: { Authorization: "Bearer " + token, "Content-Type": "application/json", ...(options.headers || {}) },
		});
		const body = await response.json().catch(() => ({}));
		return { ok: response.ok, status: response.status, body };
	}

	async #load() {
		const { ok, body } = await this.#call("status");
		this._state = ok ? body : { allowed: false, failed: true };
	}

	// One sync as a task, or settings and then data (Sync everything): Harbor only merges data into a site whose
	// settings are in sync, so the data waits until the settings are there, and a failed settings sync stops it
	async #sync(destination, what, title = "") {
		const kinds = Array.isArray(what) ? what : [what];
		const names = { settings: "Settings", data: "Data" };
		this.#stopClosing();
		this._busy = true;
		// From where to where, shown above the steps; with more than one kind, each has its own heading
		this._task = { title: title || `Sync ${kinds[0]}`, from: this.#from(), to: destination.environment, steps: [], running: true };
		let done = [];
		for (const kind of kinds) {
			if (kinds.length > 1) done = [...done, { type: "heading", text: names[kind] }];
			this._task = { ...this._task, steps: done };
			const started = await this.#call("sync", { method: "POST", body: JSON.stringify({ destination: destination.id, what: kind }) });
			if (!started.ok) {
				this._task = { ...this._task, running: false, failed: true, message: started.body?.error || "Harbor did not start the sync." };
				break;
			}
			let body = null;
			while (true) {
				await new Promise((resolve) => setTimeout(resolve, 1500));
				const answer = await this.#call("task/" + started.body.task);
				if (!answer.ok) {
					this._task = { ...this._task, running: false, failed: true, message: "Harbor did not answer." };
					break;
				}
				body = answer.body;
				this._task = { ...this._task, steps: [...done, ...(body.steps || [])], running: true };
				if (!body.running) break;
			}
			if (this._task.failed) break;
			done = this._task.steps;
			if (body.exit !== 0) {
				this._task = { ...this._task, running: false, failed: true };
				break;
			}
		}
		if (!this._task.failed) this._task = { ...this._task, running: false };
		this._busy = false;
		this._forced = new Set();
		// A sync that went well, without warnings, closes by itself after a moment, unless Cancel stops it
		if (!this._task.failed && !this._task.steps.some((step) => step.type === "warn")) this.#startClosing();
		await this.#load();
	}

	#startClosing() {
		const task = this._task;
		this._closing = true;
		this._closeTimer = setTimeout(() => {
			if (this._task === task || this._task?.title === task.title) this._task = null;
			this._closing = false;
		}, CLOSE_AFTER);
	}

	#stopClosing() {
		clearTimeout(this._closeTimer);
		this._closing = false;
	}

	#close() {
		this.#stopClosing();
		this._task = null;
	}

	// When, in words: "5 minutes ago", "yesterday", or the date
	#ago(time) {
		const seconds = Math.max(0, (Date.now() - new Date(time).getTime()) / 1000);
		if (seconds < 60) return "just now";
		if (seconds < 3600) return `${Math.round(seconds / 60)} minute${Math.round(seconds / 60) === 1 ? "" : "s"} ago`;
		if (seconds < 86400) return `${Math.round(seconds / 3600)} hour${Math.round(seconds / 3600) === 1 ? "" : "s"} ago`;
		if (seconds < 172800) return "yesterday";
		if (seconds < 30 * 86400) return `${Math.round(seconds / 86400)} days ago`;
		return new Date(time).toLocaleDateString();
	}

	// One line for each kind between two sites: when it was last synced, and whether the sending site has changed since
	#status(history, sender, receiving) {
		if (!history) return nothing;
		const names = { settings: "Settings", content: "Content", media: "Media" };
		return html`<ul class="status">${Object.entries(history).map(([kind, found]) => {
			const last = found.last;
			const state = !last ? ["never", receiving ? "Never received" : "Never synced"]
				: !last.ok ? ["failed", "Last sync failed"]
				: found.changed === true ? ["changed", "Changes not synced"]
				: found.changed === false ? ["current", "Up to date"] : ["unknown", ""];
			return html`<li class=${state[0]}>
				<span class="kind">${names[kind] || kind}</span>
				<span class="when" title=${last ? `${new Date(last.time).toLocaleString()}${last.who ? " by " + last.who : ""}` : ""}>${last ? this.#ago(last.time) : ""}</span>
				${state[1] ? html`<span class="pill" title=${state[0] === "changed" ? `${sender} has changed since it was last synced` : ""}>${state[1]}</span>` : nothing}</li>`;
		})}</ul>`;
	}

	// The sites that may sync to this one: when they last did, and whether they have changed since
	#source(source) {
		const name = (source.environment || source.id).toLowerCase();
		return html`<div class="destination source" style="--tone:${COLORS[name] || "var(--uui-color-text-alt)"}">
			<div class="name"><span class="badge"><uui-icon name=${ICONS[name] || "icon-server"}></uui-icon></span>${source.environment}</div>
			<div class="route">
				<span class="from" style="--tone:${this.#tone(source.environment)}">${source.environment}</span>
				<span class="arrow" aria-hidden="true">→</span>
				<span class="from" style="--tone:${this.#tone(this.#from())}">${this.#from()}</span>
			</div>
			${this.#status(source.history, source.environment, true)}
		</div>`;
	}

	// The newest syncs from or to this site
	#recent(recent) {
		if (!recent || !recent.length) return nothing;
		const names = (kinds) => kinds.map((kind) => kind.charAt(0).toUpperCase() + kind.slice(1)).join(", ");
		return html`<h3 class="section">Recent syncs</h3>
			<div class="recent"><table>${recent.map((entry) => html`<tr class=${entry.ok ? "" : "failed"}>
				<td class="when" title=${new Date(entry.time).toLocaleString()}>${this.#ago(entry.time)}</td>
				<td><span style="--tone:${this.#tone(entry.from)}" class="tone">${entry.from}</span> → <span style="--tone:${this.#tone(entry.to)}" class="tone">${entry.to}</span></td>
				<td>${names(entry.kinds || [])}</td>
				<td class="who">${entry.who || ""}</td>
				<td class="result">${entry.ok ? html`<uui-icon name="icon-check"></uui-icon>` : html`<uui-icon name="icon-alert"></uui-icon> Failed`}</td>
			</tr>`)}</table></div>`;
	}

	#notice(icon, title, text) {
		return html`<uui-box class="notice"><div class="notice-body"><uui-icon name=${icon}></uui-icon>
			<div><strong>${title}</strong><p>${text}</p></div></div></uui-box>`;
	}

	#destination(destination) {
		const name = (destination.environment || destination.id).toLowerCase();
		const tone = COLORS[name] || COLORS.production;
		const kinds = (destination.data || []).join(" and ");
		const link = (label, url, text) => html`<div class="link"><span class="label">${label}</span>
			<a href=${url} target="_blank" rel="noopener" title=${text}><span class="address">${text}</span><uui-icon name="icon-out"></uui-icon></a></div>`;
		return html`<div class="destination" style="--tone:${tone}">
			<div class="name"><span class="badge"><uui-icon name=${ICONS[name] || "icon-server"}></uui-icon></span>${destination.environment}</div>
			<div class="links">${destination.url
				? html`${link("Frontend", destination.url, destination.binding)}${link("Backend", destination.url + "umbraco", destination.binding + "/umbraco")}`
				: html`<span class="label">${destination.binding || destination.id}</span>`}</div>
			<div class="route" title=${`From this site (${this.#from()}) to ${destination.environment}`}>
				<span class="from" style="--tone:${this.#tone(this.#from())}">${this.#from()}</span>
				<span class="arrow" aria-hidden="true">→</span>
				<span class="to">${destination.environment}</span>
			</div>
			${this.#status(destination.history, this.#from(), false)}
			<div class="actions">${this.#buttons(destination, kinds)}</div>
		</div>`;
	}

	// Whether a kind is known to be up to date there: synced, and this site has not changed since
	#current(destination, kind) {
		return destination.history?.[kind]?.last?.ok === true && destination.history[kind].changed === false;
	}

	// The buttons a card has. What is up to date says so on its button, which is off, with "Sync … anyway" under it for
	// that one alone: Harbor only knows this site has not changed since, not whether someone changed the destination.
	// Sync everything is there whenever both may go, and skips what is up to date.
	#buttons(destination, kinds) {
		const forced = (what) => this._forced.has(`${destination.id}:${what}`) || this._forced.has(`${destination.id}:all`);
		const force = (what) => () => { this._forced = new Set([...this._forced, `${destination.id}:${what}`]); };
		const capitalKinds = kinds ? kinds.charAt(0).toUpperCase() + kinds.slice(1) : "";
		const settingsCurrent = destination.settings && this.#current(destination, "settings");
		const dataCurrent = kinds && (destination.data || []).every((kind) => this.#current(destination, kind));
		const settings = destination.settings && (forced("settings") || !settingsCurrent);
		const data = kinds && (forced("data") || !dataCurrent);
		const anyway = (text, what) => html`<button type="button" class="anyway" ?disabled=${this._busy}
			title="Harbor knows this site has not changed since. Sync anyway when something was changed there."
			@click=${force(what)}>${text}</button>`;
		if (!settings && !data) {
			const what = destination.settings && kinds ? "everything" : destination.settings ? "settings" : kinds;
			return html`<div class="nothing-to-sync"><span><uui-icon name="icon-check"></uui-icon>Nothing to sync</span>${anyway(`Sync ${what} anyway`, "all")}</div>`;
		}
		// Data only goes to a site whose settings are in sync with the main site's: Harbor tells why when they are not
		const behind = destination.settings_behind || "";
		// Sync everything: the settings where they are not up to date (or behind), then the data
		const everything = [...(settings || behind ? ["settings"] : []), ...(data ? ["data"] : [])];
		return html`
			${destination.settings ? html`<uui-button look="primary" label=${settings ? "Sync settings" : "Settings up to date"} ?disabled=${this._busy || !settings}
				title=${settings ? "Document types, templates, views, css and scripts" : "Nothing has changed in the settings here since they were synced there"}
				@click=${() => this.#sync(destination, "settings")}>${settings ? "Sync settings" : "Settings up to date"}</uui-button>
				${!settings ? html`<div class="anyway-row">${anyway("Sync settings anyway", "settings")}</div>` : nothing}` : nothing}
			${kinds ? html`<uui-button look="primary" color="positive" label=${data ? `Sync ${kinds}` : `${capitalKinds} up to date`} ?disabled=${this._busy || !data || behind}
				title=${behind ? `${behind}. Sync settings first.` : data ? `Merges ${kinds}: nothing is deleted there` : `Nothing has changed in the ${kinds} here since they were synced there`}
				@click=${() => this.#sync(destination, "data")}>${data ? `Sync ${kinds}` : `${capitalKinds} up to date`}</uui-button>
				${!data ? html`<div class="anyway-row">${anyway(`Sync ${kinds} anyway`, "data")}</div>` : nothing}` : nothing}
			${kinds && data && behind ? html`<p class="settings-first"><uui-icon name="icon-alert"></uui-icon>Sync settings first: ${
				destination.settings ? "data only goes to a site with the same document types" : behind}</p>` : nothing}
			${destination.settings && kinds ? html`<hr class="divider">
				<uui-button look="primary" class="everything" label="Sync everything" ?disabled=${this._busy || !everything.length}
					title=${everything.length === 2 ? `Settings, then ${kinds}` : everything[0] === "settings" ? `Only the settings: the ${kinds} are up to date` : `Only the ${kinds}: the settings are up to date`}
					@click=${() => this.#sync(destination, everything, "Sync everything")}>Sync everything</uui-button>` : nothing}`;
	}

	// The environment this site is, by name: Harbor tells it (an older Harbor only its id)
	#from() {
		const site = this._state.site;
		return site.environment_name || (site.environment === "main" ? "This site" : site.environment.charAt(0).toUpperCase() + site.environment.slice(1));
	}

	// This site itself: what Sync setup lets it send and receive. Receiving is marked: others may overwrite it there.
	#thisSite() {
		const site = this._state.site;
		const setup = site.setup;
		const name = this.#from();
		const capital = (word) => word.charAt(0).toUpperCase() + word.slice(1);
		const list = (part) => [...(part.settings ? ["settings"] : []), ...(part.data || [])].map(capital);
		const sends = setup ? list(setup.sends) : [];
		const receives = setup ? list(setup.receives) : [];
		const key = name.toLowerCase();
		return html`<div class="this-site" style="--tone:${this.#tone(name)}">
			<span class="badge"><uui-icon name=${ICONS[key] || "icon-server"}></uui-icon></span>
			<div class="who"><span class="label">This site</span><strong>${name}</strong></div>
			${setup ? html`<div class="flow"><span class="label">Sends</span><span>${sends.length ? sends.join(" · ") : "Nothing"}</span></div>
				<div class="flow ${receives.length ? "receives" : "none"}"><span class="label">Receives</span>
					<span>${receives.length ? receives.join(" · ") : "Nothing"}</span></div>
				${receives.length ? html`<p class="overwrite"><uui-icon name="icon-alert"></uui-icon>Other environments may overwrite this site's ${
					(receives.length > 1 ? receives.slice(0, -1).join(", ") + " and " + receives.at(-1) : receives[0]).toLowerCase()}.</p>` : nothing}` : nothing}
		</div>`;
	}

	#tone(name) {
		return COLORS[(name || "").toLowerCase()] || "var(--uui-color-text-alt)";
	}

	// One sync to every environment that may receive it, when more than one has something to sync: at the right, settings
	// rightmost. An environment known to be up to date does not count.
	#everywhere(destinations) {
		const settings = destinations.filter((destination) => destination.settings && !this.#current(destination, "settings"));
		const data = destinations.filter((destination) => (destination.data || []).length && !destination.settings_behind
			&& !destination.data.every((kind) => this.#current(destination, kind)));
		if (settings.length < 2 && data.length < 2) return nothing;
		const all = { id: "all", environment: "All environments" };
		return html`<div class="everywhere">
			<div><strong>All environments</strong><span>${settings.length > 1 ? `Settings to ${settings.map((item) => item.environment).join(", ")}` : ""}${
				settings.length > 1 && data.length > 1 ? " · " : ""}${data.length > 1 ? `data to ${data.map((item) => item.environment).join(", ")}` : ""}</span></div>
			<div class="actions">
				${data.length > 1 ? html`<uui-button look="primary" color="positive" label="Sync data to all" ?disabled=${this._busy} @click=${() => this.#sync(all, "data")}>Sync data to all</uui-button>` : nothing}
				${settings.length > 1 ? html`<uui-button look="primary" label="Sync settings to all" ?disabled=${this._busy} @click=${() => this.#sync(all, "settings")}>Sync settings to all</uui-button>` : nothing}
			</div>
		</div>`;
	}

	#taskView() {
		const task = this._task;
		if (!task) return nothing;
		const status = task.running ? "Running" : task.failed ? "Failed" : "Done";
		const last = task.steps.length - 1;
		return html`<uui-box headline=${task.title} class="task">
			<uui-tag slot="header-actions" color=${task.running ? "default" : task.failed ? "danger" : "positive"} look="primary">${status}</uui-tag>
			<div class="route task-route">
				<span class="from" style="--tone:${this.#tone(task.from)}">${task.from}</span>
				<span class="arrow" aria-hidden="true">→</span>
				<span style="--tone:${this.#tone(task.to)}" class="from">${task.to}</span>
			</div>
			${task.running && !task.steps.filter((step) => step.type !== "heading").length ? html`<uui-loader-bar></uui-loader-bar>` : nothing}
			<ol class="steps">${task.steps.map((step, index) => step.type === "heading"
				? html`<li class="heading">${step.text}</li>`
				: html`<li class=${step.type === "warn" ? "warn" : index === last && task.running ? "current" : ""}>
				${index === last && task.running ? html`<uui-loader-circle></uui-loader-circle>` : html`<uui-icon name=${step.type === "warn" ? "icon-alert" : "icon-check"}></uui-icon>`}
				<span>${step.text}</span></li>`)}</ol>
			${task.message ? html`<p class="message">${task.message}</p>` : nothing}
			${!task.running && !task.failed ? html`<p class="message">uSync on the destination imports what it received as it starts.</p>` : nothing}
			${!task.running ? html`<div class="task-end">
				${this._closing ? html`<div class="closing"><span>Closes by itself</span><span class="bar"><span style="animation-duration:${CLOSE_AFTER}ms"></span></span>
					<button type="button" class="anyway" @click=${() => this.#stopClosing()}>Cancel</button></div>` : html`<span></span>`}
				<uui-button look="secondary" label="Close" @click=${() => this.#close()}>Close</uui-button>
			</div>` : nothing}
		</uui-box>`;
	}

	render() {
		const state = this._state;
		if (!state) return html`<uui-loader-bar></uui-loader-bar>`;
		let body;
		if (state.failed) body = this.#notice("icon-alert", "VNS Harbor could not be asked", "The site could not read its own connection to Harbor.");
		else if (!state.allowed) body = this.#notice("icon-lock", "No access to VNS Harbor Sync", "An administrator can give you access by adding you to the VNS Harbor Sync user group (alias vnsHarbor). Administrators have access.");
		else if (!state.connected) body = this.#notice("icon-plug", "Not connected to Harbor", "This site is not connected to the Harbor that manages it yet. It is connected when Harbor publishes it.");
		else if (!state.reachable) body = this.#notice("icon-wifi", "Harbor cannot be reached", html`To sync from here, this site needs access to Harbor at <code>${state.harbor}</code> over HTTPS. The sync buttons are off until it has.`);
		else if (!state.site.destinations.length && !(state.site.sources || []).length) body = this.#notice("icon-settings", "Not part of any sync", "Sync setup in Harbor lets this site neither send nor receive settings or data.");
		else {
			const site = state.site;
			body = html`${this.#thisSite()}
				${site.destinations.length ? html`<h3 class="section">Sync to</h3>${this.#everywhere(site.destinations)}
					<div class="destinations">${site.destinations.map((destination) => this.#destination(destination))}</div>` : nothing}
				${this.#taskView()}
				${(site.sources || []).length ? html`<h3 class="section">Receives from</h3>
					<div class="destinations">${site.sources.map((source) => this.#source(source))}</div>` : nothing}
				${this.#recent(site.recent)}`;
		}
		return html`<div class="page">
			<header><h2>VNS Harbor Sync</h2><p>Sync this site's settings or data to its environments, and see when it was synced. Data is merged: nothing is deleted there.</p></header>
			${body}
		</div>`;
	}

	static styles = css`
		:host { display: block; padding: var(--uui-size-layout-1); }
		.page { max-width: 960px; display: grid; gap: var(--uui-size-space-5); }
		header h2 { margin: 0 0 4px; font-size: 1.4rem; }
		header p { margin: 0; color: var(--uui-color-text-alt); }
		.destinations { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--uui-size-space-4); }
		.destination { background: var(--uui-color-surface); border: 1px solid var(--uui-color-border); border-top: 3px solid var(--tone);
			border-radius: var(--uui-border-radius); padding: var(--uui-size-space-5); display: flex; flex-direction: column; gap: var(--uui-size-space-3);
			box-shadow: var(--uui-shadow-depth-1); }
		.destination .actions { display: grid; grid-template-columns: 1fr; margin-top: auto; padding-top: var(--uui-size-space-3); }
		.destination .actions uui-button { width: 100%; }
		.route { display: flex; align-items: center; gap: 8px; margin-bottom: var(--uui-size-space-1); font-size: 0.75rem; font-weight: 700;
			letter-spacing: 0.06em; text-transform: uppercase; color: var(--tone); }
		.route .from { color: var(--tone); }
		.route .arrow { color: var(--uui-color-text-alt); font-size: 1rem; letter-spacing: 0; }
		.divider { width: 100%; margin: var(--uui-size-space-3) 0; border: 0; border-top: 1px dashed var(--uui-color-border-emphasis); }
		uui-button.everything { --uui-button-background-color: #6f4fc7; --uui-button-background-color-hover: #5d3fb3;
			--uui-button-contrast: #fff; --uui-button-contrast-hover: #fff; }
		.everywhere { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--uui-size-space-4);
			background: var(--uui-color-surface); border: 1px solid var(--uui-color-border); border-radius: var(--uui-border-radius);
			padding: var(--uui-size-space-4) var(--uui-size-space-5); box-shadow: var(--uui-shadow-depth-1); }
		.everywhere strong { display: block; font-size: 1.05rem; }
		.everywhere span { color: var(--uui-color-text-alt); font-size: 0.9rem; }
		.everywhere .actions { margin-top: 0; }
		.this-site { display: flex; flex-wrap: wrap; align-items: center; gap: var(--uui-size-space-3) var(--uui-size-space-6);
			background: var(--uui-color-surface); border: 1px solid var(--uui-color-border); border-left: 4px solid var(--tone);
			border-radius: var(--uui-border-radius); padding: var(--uui-size-space-4) var(--uui-size-space-5); box-shadow: var(--uui-shadow-depth-1); }
		.this-site .who, .this-site .flow { display: grid; gap: 2px; }
		.this-site .who strong { font-size: 1.1rem; }
		.this-site .flow span:last-child { font-size: 0.95rem; }
		.this-site .flow.none span:last-child { color: var(--uui-color-text-alt); }
		.this-site .flow.receives span:last-child { color: #a86b00; font-weight: 700; }
		.this-site .overwrite { flex-basis: 100%; display: flex; align-items: center; gap: 8px; margin: 0; padding: 8px 12px;
			border-radius: var(--uui-border-radius); background: #fff4dc; color: #7a4f00; font-size: 0.9rem; }
		.section { margin: var(--uui-size-space-3) 0 calc(var(--uui-size-space-4) * -1); font-size: 0.75rem; font-weight: 700;
			letter-spacing: 0.06em; text-transform: uppercase; color: var(--uui-color-text-alt); }
		.name { display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 1.1rem; }
		.status { list-style: none; margin: 0 0 var(--uui-size-space-3); padding: 0; display: grid; gap: 6px; font-size: 0.85rem; }
		.status li { display: flex; align-items: center; gap: 8px; }
		.status .kind { font-weight: 700; min-width: 64px; }
		.status .when { color: var(--uui-color-text-alt); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
		.status .pill { margin-left: auto; padding: 1px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 700; white-space: nowrap; flex: none; }
		.status li.current .pill { background: #e3f4e8; color: #1f7a3d; }
		.status li.changed .pill { background: #fff4dc; color: #7a4f00; }
		.status li.failed .pill { background: #fde7e5; color: #a3261c; }
		.status li.never .pill { background: var(--uui-color-surface-alt); color: var(--uui-color-text-alt); }
		.source .route { margin-top: auto; }
		.nothing-to-sync { display: grid; justify-items: center; gap: 4px; margin: var(--uui-size-space-2) 0 0;
			padding: 10px; border: 1px dashed var(--uui-color-border-emphasis); border-radius: var(--uui-border-radius);
			color: var(--uui-color-text-alt); font-weight: 700; }
		.nothing-to-sync span { display: flex; align-items: center; gap: 8px; }
		.nothing-to-sync uui-icon { color: var(--uui-color-positive); }
		.anyway { padding: 0; border: 0; background: none; color: var(--uui-color-interactive); font: inherit; font-size: 0.85rem;
			font-weight: 400; text-decoration: underline; cursor: pointer; }
		.anyway:disabled { color: var(--uui-color-disabled-contrast); cursor: default; }
		.anyway-row { display: flex; justify-content: center; margin: 2px 0 var(--uui-size-space-2); }
		.settings-first { display: flex; align-items: center; gap: 6px; margin: 4px 0 0; color: #7a4f00; font-size: 0.8rem; }
		.task-end { display: flex; align-items: center; justify-content: space-between; gap: var(--uui-size-space-4); margin-top: var(--uui-size-space-4); }
		.closing { display: flex; align-items: center; gap: 10px; color: var(--uui-color-text-alt); font-size: 0.85rem; }
		.closing .bar { width: 120px; height: 4px; border-radius: 2px; background: var(--uui-color-border); overflow: hidden; }
		.closing .bar span { display: block; height: 100%; background: var(--uui-color-positive); animation: vns-close linear forwards; }
		@keyframes vns-close { from { width: 100%; } to { width: 0; } }
		.recent { background: var(--uui-color-surface); border: 1px solid var(--uui-color-border); border-radius: var(--uui-border-radius);
			box-shadow: var(--uui-shadow-depth-1); overflow-x: auto; }
		.recent table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
		.recent td { padding: 8px 14px; border-top: 1px solid var(--uui-color-border); white-space: nowrap; }
		.recent tr:first-child td { border-top: 0; }
		.recent .when, .recent .who { color: var(--uui-color-text-alt); }
		.recent .tone { color: var(--tone); font-weight: 700; }
		.recent .result { text-align: right; color: var(--uui-color-positive); }
		.recent tr.failed .result { color: var(--uui-color-danger); }
		.badge { width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center; flex: none;
			background: color-mix(in srgb, var(--tone) 16%, transparent); color: var(--tone); font-size: 1.35rem; }
		.links { display: grid; gap: 8px; font-size: 0.9rem; }
		.link { display: grid; gap: 1px; min-width: 0; }
		.label { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--uui-color-text-alt); }
		.link a { color: var(--uui-color-interactive); text-decoration: none; display: flex; align-items: center; gap: 5px; min-width: 0; }
		.link .address { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		.link a:hover { color: var(--uui-color-interactive-emphasis); text-decoration: underline; }
		.link a uui-icon { font-size: 0.8em; opacity: 0.6; flex: none; }
		.actions { display: flex; flex-wrap: wrap; gap: var(--uui-size-space-2); margin-top: var(--uui-size-space-2); }
		.notice-body { display: flex; gap: var(--uui-size-space-4); align-items: flex-start; }
		.notice-body uui-icon { font-size: 1.6rem; color: var(--uui-color-text-alt); margin-top: 2px; }
		.notice-body p { margin: 4px 0 0; color: var(--uui-color-text-alt); }
		.steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
		.steps li { display: flex; align-items: center; gap: 10px; }
		.steps li uui-icon { color: var(--uui-color-positive); }
		.steps li.warn uui-icon, .steps li.warn { color: var(--uui-color-warning-emphasis); }
		.steps li.current { font-weight: 600; }
		.steps li.heading { margin-top: var(--uui-size-space-3); font-size: 0.75rem; font-weight: 700; letter-spacing: 0.06em;
			text-transform: uppercase; color: var(--uui-color-text-alt); }
		.steps li.heading:first-child { margin-top: 0; }
		.task-route { margin-bottom: var(--uui-size-space-4); font-size: 0.85rem; }
		.message { margin: var(--uui-size-space-3) 0 0; color: var(--uui-color-text-alt); }
		code { background: var(--uui-color-surface-alt); padding: 1px 5px; border-radius: 3px; }
	`;
}

customElements.define("vns-harbor-dashboard", VnsHarborDashboard);
