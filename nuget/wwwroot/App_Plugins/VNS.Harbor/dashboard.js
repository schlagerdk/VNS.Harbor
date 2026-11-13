// VNS Harbor in the backoffice: sync this site's settings or data to its environments, through the Harbor that manages
// it. The site asks Harbor itself (VNS.Harbor's API, with the site's token); Sync setup in Harbor decides what may go.
import { LitElement, html, css, nothing } from "@umbraco-cms/backoffice/external/lit";
import { UmbElementMixin } from "@umbraco-cms/backoffice/element-api";
import { UMB_AUTH_CONTEXT } from "@umbraco-cms/backoffice/auth";

const API = "/umbraco/management/api/v1/vns-harbor/";
const COLORS = { development: "#6ca6f8", test: "#d6a54a", staging: "#a97cf2", production: "#e5574d", main: "#3fb950" };
// Each environment's own icon, in its colour
const ICONS = { development: "icon-lab", test: "icon-checkbox", staging: "icon-server-alt", production: "icon-globe", main: "icon-home" };

export default class VnsHarborDashboard extends UmbElementMixin(LitElement) {
	static properties = { _state: { state: true }, _task: { state: true }, _busy: { state: true } };

	constructor() {
		super();
		this._state = null;
		this._task = null;
		this._busy = false;
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

	async #sync(destination, what) {
		this._busy = true;
		this._task = { title: `${what === "settings" ? "Settings" : "Data"} to ${destination.environment}`, steps: [], running: true };
		const started = await this.#call("sync", { method: "POST", body: JSON.stringify({ destination: destination.id, what }) });
		if (!started.ok) {
			this._task = { ...this._task, running: false, failed: true, message: started.body?.error || "Harbor did not start the sync." };
			this._busy = false;
			return;
		}
		const id = started.body.task;
		while (true) {
			await new Promise((resolve) => setTimeout(resolve, 1500));
			const { ok, body } = await this.#call("task/" + id);
			if (!ok) {
				this._task = { ...this._task, running: false, failed: true, message: "Harbor did not answer." };
				break;
			}
			this._task = { ...this._task, steps: body.steps || [], running: body.running, failed: !body.running && body.exit !== 0 };
			if (!body.running) break;
		}
		this._busy = false;
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
			<div class="actions">
				<uui-button look="primary" ?disabled=${this._busy || !destination.settings}
					title=${destination.settings ? "Document types, templates, views, css and scripts" : "Sync setup does not let this site send settings there"}
					@click=${() => this.#sync(destination, "settings")}>Sync settings</uui-button>
				<uui-button look="primary" color="positive" ?disabled=${this._busy || !kinds}
					title=${kinds ? `Merges ${kinds}: nothing is deleted there` : "Sync setup does not let this site send data there"}
					@click=${() => this.#sync(destination, "data")}>Sync ${kinds || "data"}</uui-button>
			</div>
		</div>`;
	}

	// One sync to every environment that may receive it, when there are more than one
	#everywhere(destinations) {
		const settings = destinations.filter((destination) => destination.settings);
		const data = destinations.filter((destination) => (destination.data || []).length);
		if (settings.length < 2 && data.length < 2) return nothing;
		const all = { id: "all", environment: "all environments" };
		return html`<div class="everywhere">
			<div><strong>All environments</strong><span>${settings.length > 1 ? `Settings to ${settings.map((item) => item.environment).join(", ")}` : ""}${
				settings.length > 1 && data.length > 1 ? " · " : ""}${data.length > 1 ? `data to ${data.map((item) => item.environment).join(", ")}` : ""}</span></div>
			<div class="actions">
				<uui-button look="primary" ?disabled=${this._busy || settings.length < 2} @click=${() => this.#sync(all, "settings")}>Sync settings to all</uui-button>
				<uui-button look="primary" color="positive" ?disabled=${this._busy || data.length < 2} @click=${() => this.#sync(all, "data")}>Sync data to all</uui-button>
			</div>
		</div>`;
	}

	#taskView() {
		const task = this._task;
		if (!task) return nothing;
		const status = task.running ? "Running" : task.failed ? "Failed" : "Done";
		return html`<uui-box headline=${task.title} class="task">
			<uui-tag slot="header-actions" color=${task.running ? "default" : task.failed ? "danger" : "positive"} look="primary">${status}</uui-tag>
			${task.running && !task.steps.length ? html`<uui-loader-bar></uui-loader-bar>` : nothing}
			<ol class="steps">${task.steps.map((step, index) => html`<li class=${step.type === "warn" ? "warn" : index === task.steps.length - 1 && task.running ? "current" : ""}>
				${index === task.steps.length - 1 && task.running ? html`<uui-loader-circle></uui-loader-circle>` : html`<uui-icon name=${step.type === "warn" ? "icon-alert" : "icon-check"}></uui-icon>`}
				<span>${step.text}</span></li>`)}</ol>
			${task.message ? html`<p class="message">${task.message}</p>` : nothing}
			${!task.running && !task.failed ? html`<p class="message">uSync on the destination imports what it received as it starts.</p>` : nothing}
		</uui-box>`;
	}

	render() {
		const state = this._state;
		if (!state) return html`<uui-loader-bar></uui-loader-bar>`;
		let body;
		if (state.failed) body = this.#notice("icon-alert", "VNS Harbor could not be asked", "The site could not read its own connection to Harbor.");
		else if (!state.allowed) body = this.#notice("icon-lock", "No access to VNS Harbor", "An administrator can give you access by adding you to the VNS Harbor user group. Administrators have access.");
		else if (!state.connected) body = this.#notice("icon-plug", "Not connected to Harbor", "This site is not connected to the Harbor that manages it yet. It is connected when Harbor publishes it.");
		else if (!state.reachable) body = this.#notice("icon-wifi", "Harbor cannot be reached", html`To sync from here, this site needs access to Harbor at <code>${state.harbor}</code> over HTTPS. The sync buttons are off until it has.`);
		else if (!state.site.destinations.length) body = this.#notice("icon-settings", "Nothing to sync to", "Sync setup in Harbor does not let this site send settings or data to any of its environments.");
		else body = html`${this.#everywhere(state.site.destinations)}<div class="destinations">${state.site.destinations.map((destination) => this.#destination(destination))}</div>${this.#taskView()}`;
		return html`<div class="page">
			<header><h2>VNS Harbor</h2><p>Sync this site's settings or data to its environments. Data is merged: nothing is deleted there.</p></header>
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
		.everywhere { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--uui-size-space-4);
			background: var(--uui-color-surface); border: 1px solid var(--uui-color-border); border-radius: var(--uui-border-radius);
			padding: var(--uui-size-space-4) var(--uui-size-space-5); box-shadow: var(--uui-shadow-depth-1); }
		.everywhere strong { display: block; font-size: 1.05rem; }
		.everywhere span { color: var(--uui-color-text-alt); font-size: 0.9rem; }
		.everywhere .actions { margin-top: 0; }
		.name { display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 1.1rem; }
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
		.message { margin: var(--uui-size-space-3) 0 0; color: var(--uui-color-text-alt); }
		code { background: var(--uui-color-surface-alt); padding: 1px 5px; border-radius: 3px; }
	`;
}

customElements.define("vns-harbor-dashboard", VnsHarborDashboard);
