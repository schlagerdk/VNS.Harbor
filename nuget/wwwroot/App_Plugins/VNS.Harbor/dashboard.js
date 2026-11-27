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

	// The status is asked again when the tab is looked at again, and every half minute while it is: a change saved in
	// another section (a document type) shows as something to sync without reloading the page. Not while a sync runs.
	#refresh = () => {
		if (document.visibilityState === "visible" && this._auth && !this._busy) this.#load();
	};

	connectedCallback() {
		super.connectedCallback();
		document.addEventListener("visibilitychange", this.#refresh);
		window.addEventListener("focus", this.#refresh);
		this._timer = setInterval(this.#refresh, 30000);
		// Opened again from another section: as it is now
		if (this._auth) this.#load();
	}

	disconnectedCallback() {
		document.removeEventListener("visibilitychange", this.#refresh);
		window.removeEventListener("focus", this.#refresh);
		clearInterval(this._timer);
		super.disconnectedCallback();
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

	// A text in the user's backoffice language (lang/en.js, lang/da.js), as Umbraco picks it
	#t(key, ...args) {
		return this.localize.term("vnsHarbor_" + key, ...args);
	}

	#lang() {
		try {
			return Intl.getCanonicalLocales(this.localize.lang?.() || document.documentElement.lang || "en")[0];
		} catch {
			return "en";
		}
	}

	// A kind by name (Settings, Content, Media, Data); one Harbor knows that the texts do not keeps its own name
	#kind(kind) {
		const name = this.#t("kind_" + kind);
		return name === "vnsHarbor_kind_" + kind ? kind.charAt(0).toUpperCase() + kind.slice(1) : name;
	}

	// "a, b and c" in the user's language
	#list(items) {
		return new Intl.ListFormat(this.#lang(), { type: "conjunction" }).format(items);
	}

	#time(time) {
		return new Date(time).toLocaleString(this.#lang());
	}

	// One sync as a task, or settings and then data (Sync everything): Harbor only merges data into a site whose
	// settings are in sync, so the data waits until the settings are there, and a failed settings sync stops it
	async #sync(destination, what, title = "") {
		const kinds = Array.isArray(what) ? what : [what];
		this.#stopClosing();
		this._busy = true;
		// From where to where, shown above the steps; with more than one kind, each has its own heading
		this._task = { title: title || (kinds[0] === "settings" ? this.#t("syncSettings") : this.#t("syncKinds", this.#kind(kinds[0]).toLowerCase())), from: this.#from(), to: destination.environment, steps: [], running: true };
		let done = [];
		for (const kind of kinds) {
			if (kinds.length > 1) done = [...done, { type: "heading", text: this.#kind(kind) }];
			this._task = { ...this._task, steps: done };
			const started = await this.#call("sync", { method: "POST", body: JSON.stringify({ destination: destination.id, what: kind }) });
			if (!started.ok) {
				this._task = { ...this._task, running: false, failed: true, message: started.body?.error || this.#t("notStarted") };
				break;
			}
			let body = null;
			while (true) {
				await new Promise((resolve) => setTimeout(resolve, 1500));
				const answer = await this.#call("task/" + started.body.task);
				if (!answer.ok) {
					this._task = { ...this._task, running: false, failed: true, message: this.#t("noAnswer") };
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

	// When, in words and in the user's language: "5 minutes ago", "yesterday", or the date
	#ago(time) {
		const seconds = Math.max(0, (Date.now() - new Date(time).getTime()) / 1000);
		const words = new Intl.RelativeTimeFormat(this.#lang(), { numeric: "auto" });
		if (seconds < 60) return words.format(0, "second");
		if (seconds < 3600) return words.format(-Math.round(seconds / 60), "minute");
		if (seconds < 86400) return words.format(-Math.round(seconds / 3600), "hour");
		if (seconds < 30 * 86400) return words.format(-Math.max(1, Math.round(seconds / 86400)), "day");
		return new Date(time).toLocaleDateString(this.#lang());
	}

	// One line for each kind between two sites: when it was last synced, and whether the sending site has changed since
	#status(history, sender, receiving) {
		if (!history) return nothing;
		return html`<ul class="status">${Object.entries(history).map(([kind, found]) => {
			const last = found.last;
			const state = !last ? ["never", this.#t(receiving ? "neverReceived" : "neverSynced")]
				: !last.ok ? ["failed", this.#t("lastSyncFailed")]
				: found.changed === true ? ["changed", this.#t("changesNotSynced")]
				: found.changed === false ? ["current", this.#t("upToDate")] : ["unknown", ""];
			return html`<li class=${state[0]}>
				<span class="kind">${this.#kind(kind)}</span>
				<span class="when" title=${last ? (last.who ? this.#t("syncedBy", this.#time(last.time), last.who) : this.#time(last.time)) : ""}>${last ? this.#ago(last.time) : ""}</span>
				${state[1] ? html`<span class="pill" title=${state[0] === "changed" ? this.#t("changedSince", sender) : state[1]}>${state[1]}</span>` : html`<span></span>`}</li>`;
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
		const names = (kinds) => kinds.map((kind) => this.#kind(kind)).join(", ");
		return html`<h3 class="section">${this.#t("recentSyncs")}</h3>
			<div class="recent"><table>${recent.map((entry) => html`<tr class=${entry.ok ? "" : "failed"}>
				<td class="when" title=${this.#time(entry.time)}>${this.#ago(entry.time)}</td>
				<td><span style="--tone:${this.#tone(entry.from)}" class="tone">${entry.from}</span> → <span style="--tone:${this.#tone(entry.to)}" class="tone">${entry.to}</span></td>
				<td>${names(entry.kinds || [])}</td>
				<td class="who">${entry.who || ""}</td>
				<td class="result">${entry.ok ? html`<uui-icon name="icon-check"></uui-icon>` : html`<uui-icon name="icon-alert"></uui-icon> ${this.#t("failed")}`}</td>
			</tr>`)}</table></div>`;
	}

	#notice(icon, title, text) {
		return html`<uui-box class="notice"><div class="notice-body"><uui-icon name=${icon}></uui-icon>
			<div><strong>${title}</strong><p>${text}</p></div></div></uui-box>`;
	}

	#destination(destination) {
		const name = (destination.environment || destination.id).toLowerCase();
		const tone = COLORS[name] || COLORS.production;
		const kinds = this.#list((destination.data || []).map((kind) => this.#kind(kind).toLowerCase()));
		const link = (label, url, text) => html`<div class="link"><span class="label">${label}</span>
			<a href=${url} target="_blank" rel="noopener" title=${text}><span class="address">${text}</span><uui-icon name="icon-out"></uui-icon></a></div>`;
		return html`<div class="destination" style="--tone:${tone}">
			<div class="route route-top" title=${this.#t("routeTitle", this.#from(), destination.environment)}>
				<span class="from" style="--tone:${this.#tone(this.#from())}">${this.#from()}</span>
				<span class="arrow" aria-hidden="true">→</span>
				<span class="to">${destination.environment}</span>
			</div>
			<div class="name"><span class="badge"><uui-icon name=${ICONS[name] || "icon-server"}></uui-icon></span>${destination.environment}</div>
			<div class="links">${destination.url
				? html`${link(this.#t("frontend"), destination.url, destination.binding)}${link(this.#t("backend"), destination.url + "umbraco", destination.binding + "/umbraco")}`
				: html`<span class="label">${destination.binding || destination.id}</span>`}</div>
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
			title=${this.#t("anywayTitle")}
			@click=${force(what)}>${text}</button>`;
		if (!settings && !data) {
			const what = destination.settings && kinds ? this.#t("everything") : destination.settings ? this.#kind("settings").toLowerCase() : kinds;
			return html`<div class="nothing-to-sync"><span><uui-icon name="icon-check"></uui-icon>${this.#t("nothingToSync")}</span>${anyway(this.#t("syncWhatAnyway", what), "all")}</div>`;
		}
		// Data only goes to a site whose settings are in sync with the main site's: Harbor tells why when they are not
		const behind = destination.settings_behind || "";
		// Sync everything: the settings where they are not up to date (or behind), then the data
		const everything = [...(settings || behind ? ["settings"] : []), ...(data ? ["data"] : [])];
		return html`
			${destination.settings ? (settings ? html`<uui-button look="primary" label=${this.#t("syncSettings")} ?disabled=${this._busy}
				title=${this.#t("settingsTitle")}
				@click=${() => this.#sync(destination, "settings")}>${this.#t("syncSettings")}</uui-button>`
				: this.#upToDate(this.#t("kindUpToDate", this.#kind("settings")), anyway(this.#t("syncAnyway"), "settings"))) : nothing}
			${kinds ? (data ? html`<uui-button look="primary" color="positive" label=${this.#t("syncKinds", kinds)} ?disabled=${this._busy || behind}
				title=${behind ? this.#t("behindTitle", behind) : this.#t("mergesTitle", kinds)}
				@click=${() => this.#sync(destination, "data")}>${this.#t("syncKinds", kinds)}</uui-button>`
				: this.#upToDate(this.#t("kindUpToDate", capitalKinds), anyway(this.#t("syncAnyway"), "data"))) : nothing}
			${kinds && data && behind ? html`<p class="settings-first"><uui-icon name="icon-alert"></uui-icon><span>${this.#t("settingsFirst", behind)}</span></p>` : nothing}
			${destination.settings && kinds ? html`<hr class="divider">
				<uui-button look="primary" class="everything" label=${this.#t("syncEverything")} ?disabled=${this._busy || !everything.length}
					title=${everything.length === 2 ? this.#t("everythingBoth", kinds) : everything[0] === "settings" ? this.#t("everythingSettings", kinds) : this.#t("everythingData", kinds)}
					@click=${() => this.#sync(destination, everything, this.#t("syncEverything"))}>${this.#t("syncEverything")}</uui-button>` : nothing}`;
	}

	// What is up to date has no button: a line that says so, and a link to sync it anyway
	#upToDate(text, anyway) {
		return html`<div class="up-to-date" title=${this.#t("upToDateTitle")}>
			<uui-icon name="icon-check"></uui-icon><span>${text}</span><span class="dot" aria-hidden="true">·</span>${anyway}</div>`;
	}

	// The environment this site is, by name: Harbor tells it (an older Harbor only its id)
	#from() {
		const site = this._state.site;
		return site.environment_name || (site.environment === "main" ? this.#t("thisSite") : site.environment.charAt(0).toUpperCase() + site.environment.slice(1));
	}

	// This site itself: what Sync setup lets it send and receive. Receiving is marked: others may overwrite it there.
	#thisSite() {
		const site = this._state.site;
		const setup = site.setup;
		const name = this.#from();
		const list = (part) => [...(part.settings ? ["settings"] : []), ...(part.data || [])].map((kind) => this.#kind(kind));
		const sends = setup ? list(setup.sends) : [];
		const receives = setup ? list(setup.receives) : [];
		const key = name.toLowerCase();
		return html`<div class="this-site" style="--tone:${this.#tone(name)}">
			<span class="badge"><uui-icon name=${ICONS[key] || "icon-server"}></uui-icon></span>
			<div class="who"><span class="label">${this.#t("thisSite")}</span><strong>${name}</strong></div>
			${setup ? html`<div class="flow"><span class="label">${this.#t("sends")}</span><span>${sends.length ? sends.join(" · ") : this.#t("nothing")}</span></div>
				<div class="flow ${receives.length ? "receives" : "none"}"><span class="label">${this.#t("receives")}</span>
					<span>${receives.length ? receives.join(" · ") : this.#t("nothing")}</span></div>
				${receives.length ? html`<p class="overwrite"><uui-icon name="icon-alert"></uui-icon>${
					this.#t("overwrite", this.#list(receives.map((kind) => kind.toLowerCase())))}</p>` : nothing}` : nothing}
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
		const all = { id: "all", environment: this.#t("allEnvironments") };
		return html`<div class="everywhere">
			<div><strong>${this.#t("allEnvironments")}</strong><span>${settings.length > 1 ? this.#t("settingsTo", settings.map((item) => item.environment).join(", ")) : ""}${
				settings.length > 1 && data.length > 1 ? " · " : ""}${data.length > 1 ? this.#t("dataTo", data.map((item) => item.environment).join(", ")) : ""}</span></div>
			<div class="actions">
				${data.length > 1 ? html`<uui-button look="primary" color="positive" label=${this.#t("syncDataToAll")} ?disabled=${this._busy} @click=${() => this.#sync(all, "data")}>${this.#t("syncDataToAll")}</uui-button>` : nothing}
				${settings.length > 1 ? html`<uui-button look="primary" label=${this.#t("syncSettingsToAll")} ?disabled=${this._busy} @click=${() => this.#sync(all, "settings")}>${this.#t("syncSettingsToAll")}</uui-button>` : nothing}
			</div>
		</div>`;
	}

	#taskView() {
		const task = this._task;
		if (!task) return nothing;
		const status = this.#t(task.running ? "running" : task.failed ? "failed" : "done");
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
			${!task.running && !task.failed ? html`<p class="message">${this.#t("importsOnStart")}</p>` : nothing}
			${!task.running ? html`<div class="task-end">
				${this._closing ? html`<div class="closing"><span>${this.#t("closesByItself")}</span><span class="bar"><span style="animation-duration:${CLOSE_AFTER}ms"></span></span>
					<button type="button" class="anyway" @click=${() => this.#stopClosing()}>${this.#t("cancel")}</button></div>` : html`<span></span>`}
				<uui-button look="secondary" label=${this.#t("close")} @click=${() => this.#close()}>${this.#t("close")}</uui-button>
			</div>` : nothing}
		</uui-box>`;
	}

	render() {
		const state = this._state;
		if (!state) return html`<uui-loader-bar></uui-loader-bar>`;
		let body;
		if (state.failed) body = this.#notice("icon-alert", this.#t("failedTitle"), this.#t("failedText"));
		else if (!state.allowed) body = this.#notice("icon-lock", this.#t("noAccessTitle"), this.#t("noAccessText"));
		else if (!state.connected) body = this.#notice("icon-plug", this.#t("notConnectedTitle"), this.#t("notConnectedText"));
		else if (!state.reachable) body = this.#notice("icon-wifi", this.#t("unreachableTitle"), html`${this.#t("unreachableBefore")} <code>${state.harbor}</code> ${this.#t("unreachableAfter")}`);
		else if (!state.site.destinations.length && !(state.site.sources || []).length) body = this.#notice("icon-settings", this.#t("noSyncTitle"), this.#t("noSyncText"));
		else {
			const site = state.site;
			body = html`${this.#thisSite()}
				${site.destinations.length ? html`<h3 class="section">${this.#t("syncTo")}</h3>${this.#everywhere(site.destinations)}
					<div class="destinations">${site.destinations.map((destination) => this.#destination(destination))}</div>` : nothing}
				${this.#taskView()}
				${(site.sources || []).length ? html`<h3 class="section">${this.#t("receivesFrom")}</h3>
					<div class="destinations">${site.sources.map((source) => this.#source(source))}</div>` : nothing}
				${this.#recent(site.recent)}`;
		}
		return html`<div class="page">
			<header><h2>Harbor Sync</h2><p>${this.#t("intro")}</p></header>
			${body}
			<footer class="powered"><a href="https://vns.dk" target="_blank" rel="noopener">
				<img src="/App_Plugins/VNS.Harbor/vns-logo.png" width="30" height="21" alt="VNS - vital new software"><span>Powered by VNS</span></a></footer>
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
		/* The buttons start right under the status on every card, and Sync everything stands at the bottom of every card */
		.destination .actions { flex: 1; display: flex; flex-direction: column; flex-wrap: nowrap; gap: var(--uui-size-space-2); margin-top: 0;
			padding-top: var(--uui-size-space-3); }
		.destination .actions uui-button { width: 100%; }
		.route { display: flex; align-items: center; gap: 8px; margin-bottom: var(--uui-size-space-1); font-size: 0.75rem; font-weight: 700;
			letter-spacing: 0.06em; text-transform: uppercase; color: var(--tone); }
		.route .from { color: var(--tone); }
		.route .arrow { color: var(--uui-color-text-alt); font-size: 1rem; letter-spacing: 0; }
		/* The way a sync goes, first on the card, in a band of the destination's colour */
		.route-top { margin: calc(var(--uui-size-space-5) * -1) calc(var(--uui-size-space-5) * -1) var(--uui-size-space-2);
			justify-content: center; padding: 10px var(--uui-size-space-5); font-size: 0.85rem; background: color-mix(in srgb, var(--tone) 10%, transparent);
			border-bottom: 1px solid color-mix(in srgb, var(--tone) 25%, transparent); }
		.divider { width: 100%; margin: auto 0 var(--uui-size-space-2); padding-top: var(--uui-size-space-3); border: 0; border-bottom: 1px dashed var(--uui-color-border-emphasis); box-sizing: content-box; height: 0; }
		.destination .actions .nothing-to-sync, .destination .actions .anyway-row { margin-top: 0; }
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
		/* Three columns shared by every line: the kinds as wide as the widest (Indstillinger), the time, the state at the
		   right. The state is never wider than the card: a long one is cut, and says itself in full on hover. */
		.status { list-style: none; margin: var(--uui-size-space-3) 0; padding: 0; display: grid;
			grid-template-columns: max-content minmax(0, 1fr) auto; align-items: center; gap: 6px 10px; font-size: 0.85rem; }
		.status li { display: contents; }
		.status .kind { font-weight: 700; }
		.status .when { min-width: 0; color: var(--uui-color-text-alt); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
		.status .pill { justify-self: end; max-width: 100%; box-sizing: border-box; padding: 1px 8px; border-radius: 999px; font-size: 0.72rem;
			font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
		.up-to-date { box-sizing: border-box; min-height: 33px; display: flex; align-items: center; justify-content: center; gap: 6px; flex-wrap: wrap; padding: 2px 12px;
			border: 1px dashed var(--uui-color-border-emphasis); border-radius: var(--uui-border-radius); color: var(--uui-color-text-alt);
			font-size: 0.85rem; font-weight: 700; white-space: nowrap; }
		.up-to-date uui-icon { color: var(--uui-color-positive); }
		.settings-first { display: flex; align-items: center; gap: 6px; margin: 0; color: #7a4f00; font-size: 0.8rem; }
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
		/* Powered by VNS under it all, as the Harbor admin has it; the logo is white, made dark for the backoffice's light page */
		.powered { display: flex; justify-content: center; padding-top: var(--uui-size-space-3); }
		.powered a { display: inline-flex; align-items: center; gap: 8px; color: var(--uui-color-text-alt); font-size: 12px; font-weight: 600;
			text-decoration: none; opacity: .6; transition: opacity .2s; }
		.powered a:hover { opacity: 1; }
		.powered img { display: block; width: 30px; height: auto; filter: brightness(0); opacity: .7; }
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
