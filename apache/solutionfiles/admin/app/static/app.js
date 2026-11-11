"use strict";

const csrf = document.querySelector("meta[name=csrf]").content;
// The page the admin opens on after login: Sites, or SQL Server on a server that is mostly a SQL Server
let startView = document.querySelector("meta[name=start-view]")?.content === "sql" ? "sql" : "sites";
const $ = (selector) => document.querySelector(selector);

let backgroundScrollPosition = null;
function syncModalScrollLock() {
	const locked = !!document.querySelector("dialog[open], #dock:not([hidden])");
	if (locked && backgroundScrollPosition === null) {
		backgroundScrollPosition = { x: window.scrollX, y: window.scrollY };
		document.body.style.setProperty("--modal-scroll-top", `${-backgroundScrollPosition.y}px`);
		document.body.style.setProperty("--modal-scroll-left", `${-backgroundScrollPosition.x}px`);
		document.documentElement.classList.add("modal-scroll-locked");
	} else if (!locked && backgroundScrollPosition !== null) {
		const position = backgroundScrollPosition;
		backgroundScrollPosition = null;
		document.documentElement.classList.remove("modal-scroll-locked");
		document.body.style.removeProperty("--modal-scroll-top");
		document.body.style.removeProperty("--modal-scroll-left");
		window.scrollTo(position.x, position.y);
	}
}
new MutationObserver(syncModalScrollLock).observe(document.body, {
	subtree: true, attributes: true, attributeFilter: ["open", "hidden"],
});
syncModalScrollLock();

// Elements are always built with textContent, so file and site names can never inject HTML
function el(tag, props, ...children) {
	const node = document.createElement(tag);
	Object.assign(node, props || {});
	for (const child of children) node.append(child);
	return node;
}

function formatDate(value) {
	if (value === null || value === undefined || value === "") return "Unavailable";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) throw new Error("Invalid date: " + value);
	const pad = (number) => String(number).padStart(2, "0");
	return `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${String(date.getFullYear()).padStart(4, "0")} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatSqlSize(megabytes) {
	if (megabytes === null) return "Unavailable";
	return megabytes >= 1000 ? Number((megabytes / 1000).toFixed(2)) + " GB" : megabytes + " MB";
}

async function api(url, options = {}) {
	options.headers = Object.assign({ "X-CSRF-Token": csrf }, options.headers || {});
	if (options.json !== undefined) {
		options.method = options.method || "POST";
		options.headers["Content-Type"] = "application/json";
		options.body = JSON.stringify(options.json);
	}
	const response = await fetch(url, options);
	if (response.status === 401) {
		location.href = "/login";
		throw new Error("Not logged in");
	}
	const data = await response.json().catch(() => ({}));
	if (!response.ok) throw new Error(data.error || response.statusText);
	return data;
}

function positionToasts() {
	const container = $("#toasts");
	const dialogs = document.querySelectorAll("dialog[open]");
	const host = dialogs.length ? dialogs[dialogs.length - 1] : document.body;
	if (container.hidePopover) container.hidePopover();
	host.append(container);
	if (container.childElementCount && container.showPopover) container.showPopover();
}
document.addEventListener("close", positionToasts, true);

function toast(message, type) {
	const container = $("#toasts");
	const node = el("div", { className: "toast " + (type || "") },
		el("span", { textContent: message }));
	node.setAttribute("role", type === "error" ? "alert" : "status");
	const dismiss = () => {
		node.remove();
		if (!container.childElementCount && container.hidePopover) container.hidePopover();
	};
	if (type === "error") {
		const close = el("button", { type: "button", textContent: "Close", onclick: dismiss });
		close.setAttribute("aria-label", "Dismiss error");
		node.append(close);
		console.error(message);
	} else {
		setTimeout(dismiss, 3500);
	}
	container.append(node);
	positionToasts();
}

async function run(action) {
	try {
		return await action();
	} catch (err) {
		toast(err.message, "error");
	}
}

async function copyText(text, container) {
	if (navigator.clipboard) {
		try {
			await navigator.clipboard.writeText(text);
			return;
		} catch (error) {
			if (error.name !== "NotAllowedError" && error.name !== "SecurityError") throw error;
		}
	}
	const previous = document.activeElement;
	const selection = previous instanceof HTMLInputElement
		? { start: previous.selectionStart, end: previous.selectionEnd } : null;
	const area = el("textarea", { value: text, className: "clipboard-transfer", readOnly: true });
	container.append(area);
	try {
		area.focus();
		area.select();
		if (!document.execCommand("copy")) {
			throw new Error("Copy was blocked by the browser. Select the confirmation text and copy it manually.");
		}
	} finally {
		area.remove();
		if (previous instanceof HTMLElement) previous.focus({ preventScroll: true });
		if (selection && selection.start !== null && selection.end !== null) {
			previous.setSelectionRange(selection.start, selection.end);
		}
	}
}

let copyStatusTimer = null;

// Replaces prompt()/confirm(). Resolves to the entered text (or true without an input), or null when cancelled.
function ask({ title, message = "", value = "", placeholder = "", ok = "OK", danger = false, input = true, match = null, extra = null, checkbox = null, checkboxChecked = false, loginCheckbox = false }) {
	const dlg = $("#dlg-prompt");
	const field = $("#prompt-input");
	const extraRow = $("#prompt-extra-row");
	const extraField = $("#prompt-extra");
	const okButton = $("#prompt-ok");
	$("#prompt-title").textContent = title;
	const styledHeader = /^(add|edit|copy|create|new|change|rename|restore|site keep-alive)\b/i.test(title);
	dlg.classList.toggle("newsite", styledHeader);
	$("#prompt-header").classList.toggle("ns-head", styledHeader);
	$("#prompt-icon").hidden = !styledHeader;
	if (styledHeader) $("#prompt-icon").replaceChildren(svgIcon(
		/^(copy|restore)\b/i.test(title) ? "sync" : /^(add|create|new)\b/i.test(title) ? "file" : "pencil"));
	const messageBox = $("#prompt-message");
	const copyStatus = $("#prompt-copy-status");
	clearTimeout(copyStatusTimer);
	copyStatus.textContent = "";
	copyStatus.title = "";
	messageBox.textContent = message;
	const matchIndex = match ? message.lastIndexOf(match) : -1;
	if (matchIndex >= 0) {
		const copy = el("button", {
			type: "button", className: "confirmation-copy", textContent: match,
			title: "Copy confirmation text", ariaLabel: "Copy confirmation text: " + match,
			onclick: async () => {
				let status;
				try {
					await copyText(match, dlg);
					status = "Copied to clipboard.";
				} catch (error) {
					status = error.message;
				}
				if (!dlg.open || !copy.isConnected) return;
				if (input) field.focus({ preventScroll: true });
				clearTimeout(copyStatusTimer);
				copyStatus.textContent = status;
				copyStatus.title = status;
				copyStatusTimer = setTimeout(() => {
					copyStatus.textContent = "";
					copyStatus.title = "";
				}, 2500);
			},
		});
		messageBox.replaceChildren(message.slice(0, matchIndex), copy, message.slice(matchIndex + match.length));
	}
	$("#prompt-message").hidden = !message;
	field.hidden = !input;
	field.value = value;
	field.placeholder = match !== null ? "" : placeholder;
	extraRow.hidden = !extra;
	$("#prompt-checkbox-row").hidden = !checkbox;
	$("#prompt-login-row").hidden = !loginCheckbox;
	$("#prompt-login-checkbox").checked = loginCheckbox;
	$("#prompt-checkbox").checked = checkboxChecked;
	$("#prompt-checkbox-label").textContent = checkbox || "";
	if (extra) {
		$("#prompt-extra-label").textContent = extra.label;
		extraField.value = extra.value || "";
		extraField.placeholder = extra.placeholder || "";
	}
	okButton.value = "ok";
	okButton.textContent = ok;
	const updateConfirmation = () => { okButton.disabled = match !== null && field.value !== match; };
	updateConfirmation();
	dlg.classList.toggle("danger", danger);
	field.oninput = updateConfirmation;
	$("#form-prompt").onsubmit = (event) => {
		updateConfirmation();
		if (okButton.disabled) event.preventDefault();
	};
	$("#prompt-cancel").onclick = () => dlg.close("cancel");
	dlg.returnValue = "";
	return new Promise((resolve) => {
		dlg.onclose = () => resolve(dlg.returnValue === "ok"
			? (checkbox || loginCheckbox ? { value: field.value, checked: $("#prompt-checkbox").checked, deleteLogin: $("#prompt-login-checkbox").checked } : extra ? { value: field.value, extra: extraField.value } : input ? field.value : true)
			: null);
		dlg.showModal();
		if (input) { field.focus(); field.select(); } else if (extra) extraField.focus(); else okButton.focus();
	});
}

// ------------------------------------------------------------------ views

let remoteFilesSite = "";
let serverFiles = false;
let localFilesRoot = "";
let currentView = "sites";
const views = ["sites", "site", "sql"];

function showView(name) {
	currentView = name;
	// The site search belongs to the sites: it is hidden on the SQL page
	document.body.dataset.view = name;
	// The switch in the header shows which of the two sections is open
	for (const button of document.querySelectorAll(".view-switch button")) {
		const active = button.dataset.view === (name === "sql" ? "sql" : "sites");
		button.classList.toggle("active", active);
		button.setAttribute("aria-pressed", String(active));
	}
	for (const view of views) $("#view-" + view).hidden = view !== name;
	if (name === "sites" && $("#dock").hidden && !$("#search").hidden) $("#search").focus({ preventScroll: true });
}

// Hash routes: #sites, #site/<name>, #files/<path>
async function route() {
	const hash = decodeURIComponent(location.hash.slice(1));
	const slash = hash.indexOf("/");
	const view = slash < 0 ? hash : hash.slice(0, slash);
	const arg = slash < 0 ? "" : hash.slice(slash + 1);
	if (editorDialog.open && !(await discardOk())) {
		history.replaceState(null, "", currentView === "site" ? siteLink($("#site-head h2").textContent) : "#sites");
		return;
	}
	if (editorDialog.open) {
		openFile = null;
		setDirty(false);
		editorDialog.close();
	}
	if (view === "files" || view === "remote-files") {
		const parts = arg.split("/");
		const remote = view === "remote-files" ? parts.shift() : "";
		history.replaceState(null, "", "#sites");
		showView("sites");
		await run(() => openFiles(view === "remote-files" ? parts.join("/") : arg, remote));
	} else if (view === "sql") {
		if (filesDialog.open) filesDialog.close();
		showView("sql");
		await run(loadSqlConnections);
	} else if (view === "site" && arg) {
		if (filesDialog.open) filesDialog.close();
		showView("site");
		// A site's page always opens at its top, wherever the list was scrolled to
		window.scrollTo(0, 0);
		renderSite(arg);
	} else if (!view && startView === "sql") {
		// The page opened after login, on a server where SQL Server is the main thing
		history.replaceState(null, "", "#sql");
		await route();
	} else {
		if (filesDialog.open) filesDialog.close();
		showView("sites");
	}
}
window.addEventListener("hashchange", route);
const go = (hash) => { location.hash = hash; };
const siteLink = (name) => "#site/" + encodeURIComponent(name);
const fsUrl = (action) => remoteFilesSite ? "/api/remote-fs/" + encodeURIComponent(remoteFilesSite) + "/" + action : (serverFiles ? "/api/server-fs/" : "/api/site-fs/" + localFilesRoot.split("/").map(encodeURIComponent).join("/") + "/") + action;

// ------------------------------------------------------------------ sites

let allSites = [];
// The admin site itself is reached via Settings.
const isListed = (s) => !s.admin;
const listedSites = () => allSites.filter(isListed);
let filter = "all";
let query = "";

const kindLabel = { umbraco: "Umbraco", csharp: "C# / .NET", python: "Python", html: "HTML" };

let defaultUmbracoVersion = "";
let sitesLoaded = false;
let updateCheckTimer = null;
let updateCheckPending = new Set();
let updateCheckFeedback = false;
const updateCheckCurrent = new Map();
function renderCardUpdateChecks() {
	for (const node of document.querySelectorAll(".card-update-check")) {
		const name = node.dataset.site;
		const checking = updateCheckPending.has(name);
		const current = (updateCheckCurrent.get(name) || 0) > Date.now();
		node.hidden = !checking && !current;
		node.classList.toggle("up-to-date", !checking && current);
		node.replaceChildren(...(checking ? [el("span", { className: "activity-spinner", ariaHidden: "true" })] : []),
			el("span", { textContent: checking ? "Checking updates…" : "All is up to date" }));
	}
}
function monitorUpdateCheck(state) {
	if (state.running) updateCheckFeedback = true;
	updateCheckPending = new Set(state.running ? state.pending || [] : []);
	for (const [name, result] of Object.entries(state.results || {})) {
		if (updateCheckFeedback && result === "current" && !updateCheckCurrent.has(name)) {
			updateCheckCurrent.set(name, Date.now() + 4000);
			setTimeout(renderCardUpdateChecks, 4000);
		}
	}
	renderCardUpdateChecks();
	const button = $("#btn-check-updates");
	button.disabled = state.running;
	button.replaceChildren(svgIcon("download"), el("span", { textContent: state.running ? `Checking updates (${state.completed}/${state.total})…` : "Check for updates" }));
	if (state.running && !updateCheckTimer) {
		updateCheckTimer = setTimeout(() => {
			updateCheckTimer = null;
			run(async () => {
				let result;
				try {
					result = await api("/api/package-updates");
				} catch (error) {
					monitorUpdateCheck({ running: false });
					throw error;
				}
				monitorUpdateCheck(result);
				if (!result.running) {
					await loadSites();
					if (result.errors.length) toast("Update checks failed: " + result.errors.join(", "), "error");
					else toast(`Checked ${result.total} sites for package updates.`, "ok");
				}
			});
		}, 2000);
	}
}

async function loadSites() {
	const data = await api("/api/sites");
	allSites = data.sites;
	sitesLoaded = true;
	if (data.update_check) monitorUpdateCheck(data.update_check);
	$("#settings-admin-site").hidden = !allSites.some((s) => s.admin);
	$("#settings-admin-group").hidden = false;
	defaultUmbracoVersion = (data.defaults || {}).umbraco_version || "";
	renderSites();
	if (currentView === "site") renderSite(decodeURIComponent(location.hash.slice(6)));
	if (currentView === "sql") await refreshSql();
}

// A site is running when any of its services is; sites without a service are static
function siteState(site) {
	const units = site.environments.filter((e) => e.unit);
	if (!units.length) return "";
	return units.some((e) => e.state === "active") ? "active" : "inactive";
}
const isTarget = (env) => env.key !== "main";
const hasProd = (site) => site.environments.some(isTarget);
const targetFields = (env) => (env && env.target && env.target !== "prod" ? { target: env.target } : {});
const remoteRef = (site, env) => (env.target && env.target !== "prod" ? `${site.name}@${env.target}` : site.name);
// What people recognise an environment by: its binding and the server it is on, not the internal site@id
function remoteLabel(ref) {
	const [name, target = "prod"] = ref.split("@");
	const site = allSites.find((item) => item.name === name);
	const env = site?.environments.find((item) => item.target === target);
	if (!env) return ref;
	return `${bindingLabel(env) || name} @ ${env.host || "?"}`;
}

// Environments are always listed as Development, Test, Staging, Production. Anything else comes last.
const environmentOrder = ["development", "test", "staging", "production"];
function environmentRank(env) {
	const rank = environmentOrder.indexOf((env.environment || "").toLowerCase());
	return rank < 0 ? environmentOrder.length : rank;
}
// Within an environment, alphabetically by binding, so a site is easy to find
const environmentName = (env) => (env.display_binding || env.binding || env.folder || env.unit || "").toLowerCase();
const sortedEnvironments = (environments) => [...environments].sort((a, b) =>
	environmentRank(a) - environmentRank(b) || environmentName(a).localeCompare(environmentName(b), "da"));

function renderStats() {
	const searched = overviewSites().filter(matchesQuery);
	const count = (test) => searched.filter(test).length;
	const chips = [
		["all", "All", searched.length],
		["running", "Running", count((s) => siteState(s) === "active")],
		["stopped", "Stopped", count((s) => siteState(s) === "inactive")],
		["prod", "With environments", count(hasProd)],
	];
	for (const kind of Object.keys(kindLabel)) {
		const n = count((s) => s.kind === kind);
		if (n) chips.push([kind, kindLabel[kind], n]);
	}
	const usefulChips = chips.filter(([key, , n]) => key === "all" || (n > 0 && n < searched.length));
	// What is waiting to be done, shown whenever a site has it, even when every site has it
	const pending = Object.entries(attentionFilters)
		.map(([key, [label, test, title]]) => [key, label, count(test), title])
		.filter(([, , n]) => n > 0);
	if (![...usefulChips, ...pending].some(([key]) => key === filter)) filter = "all";
	const box = $("#stats");
	box.hidden = listedSites().length <= 1 || (usefulChips.length <= 1 && !pending.length);
	box.replaceChildren();
	const chip = ([key, label, n, title], className = "") => el("div", { className: "chip" + className + (filter === key ? " active" : ""), title: title || "", onclick: () => { filter = key; renderSites(); } },
		el("b", { textContent: n }), el("span", { textContent: label }));
	for (const item of usefulChips) box.append(chip(item));
	if (pending.length) {
		box.append(el("span", { className: "chip-divider", ariaHidden: "true" }));
		for (const item of pending) box.append(chip(item, " attention"));
	}
}

// Filters for sites that need something done
const attentionFilters = {
	pending: ["Publish pending", (s) => s.environments.some((env) => env.publish_status === "pending"),
		"The project changed since an environment was last published"],
	updates: ["Updates available", (s) => s.updates === "available", "NuGet package updates were found"],
	"check-failed": ["Update check failed", (s) => s.updates === "error", "The last package update check failed"],
	"not-set-up": ["Not set up", (s) => !!s.not_set_up, "A folder without a service, or a site whose setup was not finished"],
	missing: ["Missing environment", (s) => s.environments.some(isMissingEnv), "An environment no longer exists on its server"],
};


function matchesQuery(site) {
	const text = query.trim().toLowerCase();
	const haystack = site.name + " " + siteLabel(site) + " " + site.environments.map((e) => e.binding + " " + bindingLabel(e) + " " + (e.host || "")).join(" ");
	return !text || haystack.toLowerCase().includes(text);
}

function matches(site) {
	const state = siteState(site);
	if (filter === "running" && state !== "active") return false;
	if (filter === "stopped" && state !== "inactive") return false;
	if (filter === "prod" && !hasProd(site)) return false;
	if (kindLabel[filter] && site.kind !== filter) return false;
	if (attentionFilters[filter] && !attentionFilters[filter][1](site)) return false;
	return matchesQuery(site);
}


// ------------------------------------------------------------------ button icons
// Inline SVG (Lucide-style, 24x24 stroke icons); the CSP does not allow external images
const iconPaths = {
	grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
	rows: "M4 5h16v4H4zM4 15h16v4H4z",
	lock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
	unlock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 7.5-2",
	terminal: "M4 17l6-6-6-6M12 19h8",
	folder: "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z",
	back: "M19 12H5M12 19l-7-7 7-7",
	upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12",
	download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
	user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
	logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
	sync: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8M21 3v5h-5M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16M8 16H3v5",
	restart: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8M21 3v5h-5",
	trash: "M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2",
	play: "M6 3l14 9-14 9z",
	stop: "M7 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z",
	file: "M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7ZM14 2v4a2 2 0 0 0 2 2h4M8 13h8M8 17h8",
	globe: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20ZM2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z",
	shield: "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1zM9 12l2 2 4-4",
	sliders: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M2 14h4M10 8h4M18 16h4",
	activity: "M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2",
	clock: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20ZM12 6v6l4 2",
	pencil: "M21.17 6.81a2.12 2.12 0 0 0-3-3L3.84 17.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5zM15 5l4 4",
	eraser: "M7 21l-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21M22 21H7M5 11l9 9",
	manage: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
	external: "M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6",
	key: "M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4",
	server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01M12 7h5M12 17h5",
	package: "m12 3 9 5v8l-9 5-9-5V8zM3 8l9 5 9-5M12 13v8M7.5 5.5l9 5",
};

// label (exact, or a prefix ending in "*") -> [icon, tone]
const buttonStyles = {
	"Shell": ["terminal", "blue"], "Server shell": ["terminal", "blue"], "New file": ["file", "blue"], "New folder": ["folder", "blue"], "Upload": ["upload", "blue"], "Browse /": ["folder", "blue"], "Files": ["folder", "blue"], "Manage": ["manage", "blue"],
	"Publish": ["upload", "green"], "Publish all": ["upload", "green"], "Publish environments": ["upload", "green"], "Sync settings…": ["sync", "blue"], "Start": ["play", "green"],
	"Check for updates": ["download", "amber"],
	"Add package": ["download", "green"], "Remove package": ["trash", "red"],
	"Add": ["download", "green"], "Remove": ["trash", "red"], "Check": ["download", "amber"],
	"Fix permissions": ["shield", "teal"], "Update Python packages": ["sync", "blue"],
	"Add environment": ["server", "purple"],
	"Move under main site": ["server", "purple"], "Set up site…": ["server", "green"],
	"Migrate…": ["server", "purple"],
	"Check outdated": ["download", "amber"], "Restart": ["restart", "amber"], "Restart Apache": ["restart", "amber"],
	"Stop": ["stop", "red"], "Clear logs": ["eraser", "gray"], "Logs": ["file", "gray"],
	"SQL Server": ["server", "purple"], "Web server": ["globe", "blue"], "Preferences…": ["sliders", "purple"], "Files (/)": ["folder", "blue"],
	"Test config": ["activity", "teal"], "Error logs": ["file", "teal"],
	"Admin app log": ["file", "gray"], "Apache error logs": ["file", "teal"],
	"Copy site": ["file", "blue"],
	"Issue certificate": ["shield", "teal"], "Change environment": ["sliders", "purple"], "Sync setup…": ["sliders", "teal"], "Sync data…": ["sync", "pink"], "Change bindings": ["globe", "blue"],
	"Check service": ["activity", "teal"], "Test Apache config": ["activity", "teal"], "Admin site": ["manage", "gray"],
	"Remove environment": ["server", "amber"], "Edit service": ["pencil", "amber"], "Edit config": ["pencil", "amber"], "Edit web.config": ["pencil", "amber"], "Edit appsettings.json": ["pencil", "amber"], "Delete*": ["trash", "red"],
};

function svgIcon(name) {
	const ns = "http://www.w3.org/2000/svg";
	const svg = document.createElementNS(ns, "svg");
	for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", class: "ico", "aria-hidden": "true" })) svg.setAttribute(key, value);
	const path = document.createElementNS(ns, "path");
	path.setAttribute("d", iconPaths[name]);
	svg.append(path);
	return svg;
}

for (const icon of document.querySelectorAll("[data-header-icon]")) {
	icon.replaceChildren(svgIcon(icon.dataset.headerIcon));
}

function decorate(root) {
	for (const button of root.querySelectorAll("button:not([data-tone])")) {
		const label = button.textContent.trim();
		const style = buttonStyles[label] || (label.startsWith("Delete") ? buttonStyles["Delete*"] : null);
		if (!style) continue;
		button.dataset.tone = style[1];
		button.prepend(svgIcon(style[0]));
	}
}

let multiSelecting = false;
const multiSites = new Set();
const canMultiSelect = (site) => !site.admin && site.has_project && (site.kind === "csharp" || site.kind === "umbraco");
const overviewSites = () => listedSites().filter((site) => !multiSelecting || canMultiSelect(site));

function renderMultiSelection() {
	const multipleProjects = listedSites().filter(canMultiSelect).length > 1;
	if (!multipleProjects) {
		multiSelecting = false;
		multiSites.clear();
	}
	$("#btn-check-updates").hidden = !multipleProjects;
	$("#btn-multi").hidden = multiSelecting || !multipleProjects;
	// Choose action appears once a site is chosen; Select all chooses every site shown, or none again
	$("#btn-multi-action").hidden = !multiSelecting || multiSites.size === 0;
	const shownSites = overviewSites().filter(matches);
	$("#btn-multi-all").hidden = !multiSelecting || !shownSites.length;
	$("#btn-multi-all").textContent = shownSites.length && shownSites.every((site) => multiSites.has(site.name)) ? "Select none" : "Select all";
	$("#btn-multi-action").disabled = multiSites.size === 0;
	$("#btn-multi-action").textContent = `Choose action (${multiSites.size})`;
	$("#btn-multi-cancel").hidden = !multiSelecting;
}

// The overview as cards in a grid (the default) or as a list with a row per site, remembered in this browser
let siteLayout = "grid";
try { if (localStorage.getItem("vns-site-layout") === "list") siteLayout = "list"; } catch (error) {}
function applySiteLayout() {
	$("#sites").classList.toggle("list", siteLayout === "list");
	$("#layout-grid").setAttribute("aria-pressed", String(siteLayout === "grid"));
	$("#layout-list").setAttribute("aria-pressed", String(siteLayout === "list"));
}
$("#layout-grid").append(svgIcon("grid"));
$("#layout-list").append(svgIcon("rows"));
for (const layout of ["grid", "list"]) {
	$("#layout-" + layout).onclick = () => {
		siteLayout = layout;
		try { localStorage.setItem("vns-site-layout", layout); } catch (error) {}
		renderSites();
	};
}

function renderSites() {
	const siteCount = listedSites().length;
	const hideFilters = siteCount <= 1;
	if (hideFilters) {
		filter = "all";
		query = "";
		$("#search").value = "";
	}
	$("#search").hidden = hideFilters;
	renderStats();
	for (const name of multiSites) {
		if (!listedSites().some((site) => site.name === name && canMultiSelect(site))) multiSites.delete(name);
	}
	renderMultiSelection();
	const box = $("#sites");
	applySiteLayout();
	box.replaceChildren();
	const visible = overviewSites().filter(matches);
	$("#no-sites").hidden = visible.length > 0;
	$("#btn-new-site").hidden = visible.length === 0;
	const firstSite = siteCount === 0;
	$("#empty-sites-title").textContent = firstSite ? "Your first site starts here" : "Make room for your next idea";
	$("#empty-sites-description").textContent = firstSite
		? "Create a home for your next idea. Set up your first site in just a few steps."
		: "Try a different search or filter, or bring something new to life by creating a site.";
	for (const site of visible) box.append(siteCard(site));
	updateSiteActivity();
	renderCardUpdateChecks();
	decorate(box);
}

function updateSiteActivity() {
	for (const badge of document.querySelectorAll(".site-activity")) {
		const tasks = Object.values(terminals).filter((task) => !task.exited && task.sites.includes(badge.dataset.site)
			&& (!badge.dataset.scope || task.scope === "all" || task.scope === badge.dataset.scope)
			// An environment's card only shows the tasks that work on it
			&& (!badge.dataset.target || !task.targets?.length || task.targets.includes(badge.dataset.target)));
		badge.hidden = tasks.length === 0;
		badge.title = tasks.map((task) => task.taskTitle).join("\n");
	}
}

function siteActivity(site, scope = "", target = "") {
	const activity = el("span", { className: "site-activity", hidden: true, role: "status" },
		el("span", { className: "activity-spinner", ariaHidden: "true" }), el("span", { textContent: "Working" }));
	activity.dataset.site = site.name;
	activity.dataset.scope = scope;
	if (target) activity.dataset.target = target;
	return activity;
}

// Opens a URL in a new tab: a plain link, or (asButton) a small button with an "open" icon
function openLink(text, url, title, asButton) {
	const a = el("a", { className: asButton ? "btn-link" : "link", href: url, target: "_blank", rel: "noopener noreferrer", title: title || url, onclick: (event) => event.stopPropagation() });
	if (!asButton) { a.textContent = text; return a; }
	a.append(el("span", { textContent: text }), svgIcon("external"));
	return a;
}
const bindingLink = (binding, asButton) => binding
	? openLink(binding, "https://" + binding + "/", "", asButton)
	: el("span", { className: "muted", textContent: "no binding" });
const backofficeLink = (binding, asButton) => openLink(binding + "/umbraco", "https://" + binding + "/umbraco", "Umbraco backoffice", asButton);
const siteLabel = (site) => site.display_name || site.name;
const bindingLabel = (env) => env.display_binding || env.binding;
// The name people look for. The folder can have another name, and is listed on the page.
const siteTitle = (site) => {
	const main = site.environments?.find((env) => env.key === "main");
	return (main && bindingLabel(main)) || siteLabel(site);
};
const aliasLabels = (env) => env?.display_aliases || env?.aliases || [];

function environmentAddress(env) {
	const hostname = env.binding || (env.mode === "remote" ? env.host : $("meta[name=server-hostname]")?.content) || "";
	const authority = hostname.includes(":") && !hostname.startsWith("[") ? `[${hostname}]` : hostname;
	return {
		hostname,
		url: hostname ? `${env.scheme || "https"}://${authority}/` : "",
		title: env.binding ? bindingLabel(env) : `Default / wildcard: ${hostname}. Apache chooses the site for this address.`,
	};
}

const versionColors = 8; // majors 18, 19, 20 … get different colours (see .badge.version[data-major] in style.css)

// .NET 10.0 is shown as .NET 10, a minor version only when it is not .0
const dotnetLabel = (version) => ".NET " + String(version).replace(/\.0$/, "");
// Each major version its own colour (see .badge.dotnet[data-major] in style.css), so sites on an old .NET stand out
function dotnetBadge(version, title) {
	const badge = el("span", { className: "badge dotnet", textContent: dotnetLabel(version), title });
	const major = parseInt(version, 10);
	if (Number.isFinite(major)) badge.dataset.major = String(major);
	return badge;
}

function kindBadges(site) {
	const badges = el("div", { className: "badges" }, el("span", { className: "badge " + site.kind, textContent: kindLabel[site.kind] || site.kind }));
	if (site.umbraco_version) {
		const major = parseInt(site.umbraco_version, 10);
		const badge = el("span", { className: "badge version", textContent: "v" + site.umbraco_version, title: "Umbraco " + site.umbraco_version });
		if (Number.isFinite(major)) badge.dataset.major = String(major % versionColors);
		badges.append(badge);
	}
	if (site.dotnet) badges.append(dotnetBadge(site.dotnet, `Runs on .NET ${site.dotnet}`));
	if (site.admin) badges.append(el("span", { className: "badge admin", textContent: "this admin site" }));
	if (site.not_set_up) badges.append(el("span", { className: "badge not-set-up", textContent: "Not set up", title: "A folder without a service, or a site whose setup was not finished. Open it and choose Set up site." }));
	if (site.managed_by) badges.append(el("span", { className: "badge", textContent: "Managed from " + (site.managed_by.host || "another server"), title: `Environment of ${site.managed_by.site} on ${site.managed_by.host}. Publish and copy it from there.` }));
	if (site.updates === "available") badges.append(el("span", { className: "badge updates-available", textContent: "Updates available", title: "NuGet updates were found. Install package updates, then publish the site." }));
	if (site.updates === "error") badges.append(el("span", { className: "badge updates-error", textContent: "Update check failed", title: "Run Check for updates again. See server logs for details." }));
	return badges;
}

function httpsLock(secure) {
	const label = secure == null ? "HTTPS status unavailable" : secure ? "HTTPS enabled" : "HTTPS not enabled";
	return el("span", { className: "https-lock " + (secure == null ? "unknown" : secure ? "secure" : "insecure"), title: label, ariaLabel: label }, svgIcon(secure ? "lock" : "unlock"));
}

const isRemoteEnv = (env) => env.mode === "remote" || env.mode === "windows";
// The server answered, and the site's folder is gone. Without an answer, exists is not set.
const isMissingEnv = (env) => env.mode === "remote" && env.exists === false;

// Windows logo and Tux, drawn in their own colours beside the remote icon
const osLogos = {
	windows: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#4cc2ff" d="M3 5.4 10.4 4.4v7.1H3zM11.4 4.2 21 3v8.5h-9.6zM3 12.5h7.4v7.1L3 18.6zM11.4 12.5H21V21l-9.6-1.3z"/></svg>',
	linux: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#2d333b" stroke="#c9d1d9" stroke-width="1" d="M12 2.5c-2.6 0-4 2.2-4 4.8 0 1.6-.6 2.7-1.6 4.2C5.2 13.3 4.5 15 4.8 17c.3 2.2 2.6 3.6 7.2 3.6s6.9-1.4 7.2-3.6c.3-2-.4-3.7-1.6-5.5-1-1.5-1.6-2.6-1.6-4.2 0-2.6-1.4-4.8-4-4.8z"/><ellipse cx="12" cy="15" rx="3.6" ry="4.6" fill="#f0f6fc"/><circle cx="10.6" cy="7.2" r="1" fill="#f0f6fc"/><circle cx="13.4" cy="7.2" r="1" fill="#f0f6fc"/><circle cx="10.7" cy="7.4" r=".45" fill="#0d1117"/><circle cx="13.3" cy="7.4" r=".45" fill="#0d1117"/><path fill="#f2b01e" d="M10.4 9.2c.5-.6 2.7-.6 3.2 0 .2.3-.8 1.4-1.6 1.4s-1.8-1.1-1.6-1.4zM5.2 19.2c.4-1.2 2.4-1.6 3.6-1 .9.5.9 1.9.2 2.6-.9.8-3.4.6-3.8-.3-.1-.4-.1-.9 0-1.3zM18.8 19.2c-.4-1.2-2.4-1.6-3.6-1-.9.5-.9 1.9-.2 2.6.9.8 3.4.6 3.8-.3.1-.4.1-.9 0-1.3z"/></svg>',
};
function remoteIcon(env) {
	const os = env.mode === "windows" ? "windows" : "linux";
	const label = `Remote ${os === "windows" ? "Windows" : "Linux"} server: ${env.host}`;
	const icon = el("span", { className: "remote-icon", title: label, ariaLabel: label }, svgIcon("server"));
	icon.append(el("span", { className: "os-logo os-" + os, innerHTML: osLogos[os] }));
	return icon;
}

function environmentLabel(site, env) {
	return site.kind === "python" ? "Live" : env.environment || (site.kind === "csharp" || site.kind === "umbraco" ? "Unknown" : env.label);
}

function publishBadge(env) {
	const labels = { pending: "Publish pending", unknown: "Publish status unknown" };
	const label = labels[env.publish_status];
	return label ? el("span", { className: "badge publish-" + env.publish_status, textContent: label,
		title: env.publish_status === "pending" ? "Project or package configuration has changed since this environment's last successful publish." : "Publish this environment to establish its package baseline." }) : null;
}

const maxCardEnvironments = 2;
// Cards the user has unfolded. Kept while the page is open, also when the list is drawn again.
const expandedCards = new Set();

// One line: the environment, a button with the binding that opens the site, and the backoffice
function compactEnvironmentRow(site, env, environment) {
	const row = el("div", { className: "site-link-group compact" + (isTarget(env) ? " production" : ""), role: "group", ariaLabel: environment + " · " + (bindingLabel(env) || siteLabel(site)) });
	const address = environmentAddress(env);
	const head = el("div", { className: "site-environment-head" },
		httpsLock(env.https),
		el("span", { className: "badge env-" + environment.toLowerCase(), textContent: environment }));
	if (isRemoteEnv(env)) head.append(remoteIcon(env));
	row.append(head);
	if (isMissingEnv(env)) row.append(el("span", { className: "badge env-missing", textContent: "Not found on server", title: `The site no longer exists on ${env.host}.` }));
	else if (env.publish_status === "pending") {
		// A card has little room: there it is an icon, with the words as its title. The list has room for them.
		const badge = publishBadge(env);
		if (siteLayout === "list") row.append(badge);
		else head.append(el("span", { className: "publish-pending-icon", title: "Publish pending: " + badge.title, ariaLabel: "Publish pending" }, svgIcon("upload")));
	}
	if (address.url) {
		const frontend = openLink(bindingLabel(env) || address.hostname, address.url, environment + ": " + address.title, true);
		frontend.classList.add("frontend");
		const links = el("div", { className: "site-environment-links" }, frontend);
		if (site.kind === "umbraco") {
			const backend = openLink("Backend", address.url + "umbraco", environment + " backoffice: " + address.title, true);
			backend.classList.add("backend");
			links.append(backend);
		}
		row.append(links);
	} else {
		row.append(el("span", { className: "muted", textContent: "No binding" }));
	}
	return row;
}

function siteCard(site) {
	const state = siteState(site);
	const actions = el("div", { className: "site-links" });
	const allEnvironments = sortedEnvironments(site.environments);
	const compact = allEnvironments.length > 1 || siteLayout === "list";
	const expanded = expandedCards.has(site.name);
	const listed = compact && !expanded ? allEnvironments.slice(0, maxCardEnvironments) : allEnvironments;
	for (const env of listed) {
		const production = isTarget(env);
		const environment = environmentLabel(site, env);
		if (compact) {
			const row = compactEnvironmentRow(site, env, environment);
			actions.append(row);
			continue;
		}
		const group = el("div", { className: "site-link-group" + (production ? " production" : ""), role: "group", ariaLabel: environment + " · " + (bindingLabel(env) || siteLabel(site)) });
		const heading = el("div", { className: "site-environment-head" },
			httpsLock(env.https),
			el("span", { className: "badge env-" + environment.toLowerCase(), textContent: environment }));
		if (isRemoteEnv(env)) heading.append(remoteIcon(env));
		const pending = publishBadge(env);
		if (pending) heading.append(el("div", { className: "publish-status" }, pending));
		group.append(heading);
		const links = el("div", { className: "site-environment-links" });
		const address = environmentAddress(env);
		if (!address.url) {
			group.append(el("span", { className: "muted", textContent: "No binding configured" }));
			actions.append(group);
			continue;
		}
		const frontend = openLink("Frontend", address.url, environment + ": " + address.title, true);
		frontend.classList.add("frontend");
		links.append(frontend);
		if (site.kind === "umbraco") {
			const backend = openLink("Backend", address.url + "umbraco", environment + " backoffice: " + address.title, true);
			backend.classList.add("backend");
			links.append(backend);
		}
		group.append(links);
		actions.append(group);
	}

	if (allEnvironments.length > listed.length) {
		actions.append(el("button", { type: "button", className: "site-link-more", textContent: `+${allEnvironments.length - listed.length} more`,
			ariaExpanded: "false", onclick: (event) => { event.stopPropagation(); expandedCards.add(site.name); renderSites(); } }));
	} else if (expanded && allEnvironments.length > maxCardEnvironments) {
		actions.append(el("button", { type: "button", className: "site-link-more", textContent: "Show fewer",
			ariaExpanded: "true", onclick: (event) => { event.stopPropagation(); expandedCards.delete(site.name); renderSites(); } }));
	}

	const badges = kindBadges(site);
	badges.append(siteActivity(site));
	const updateStatus = el("span", { className: "card-update-check", hidden: true, role: "status" });
	updateStatus.dataset.site = site.name;
	badges.append(updateStatus);
	const selection = multiSelecting && canMultiSelect(site)
		? el("label", { className: "multi-select", onclick: (event) => event.stopPropagation() },
			el("input", { type: "checkbox", checked: multiSites.has(site.name), ariaLabel: "Select " + siteLabel(site),
				onchange: (event) => {
					if (event.target.checked) multiSites.add(site.name); else multiSites.delete(site.name);
					renderMultiSelection();
				} }), el("span", { textContent: "Select site" }))
		: null;
	// While sites are being selected, a click anywhere on the card selects it; links on it still open
	const open = () => {
		if (!selection) return go(siteLink(site.name));
		const box = selection.querySelector("input");
		box.checked = !box.checked;
		box.dispatchEvent(new Event("change"));
	};
	return el("article", { className: "site " + state + (hasProd(site) ? " with-production" : "") + (site.admin ? " is-admin" : "") + (selection ? " selectable" : ""), onclick: open },
		...(selection ? [selection] : []),
		el("div", { className: "site-head" },
			el("span", { className: "site-emblem" }, svgIcon("globe")),
			el("div", { className: "site-identity" }, el("h3", { className: "site-title", textContent: siteTitle(site), title: site.name }), badges),
			el("span", { className: "dot " + state, title: state || "Static site" })),
		actions);
}

function customScriptGroups(files) {
	const titleWords = (words) => words.map((word) => {
		const [first, ...rest] = Array.from(word);
		return first ? first.toUpperCase() + rest.join("") : "";
	}).join(" ");
	const groups = new Map();
	for (const file of files) {
		const parts = file.replace(/\.sh$/, "").split(/[_-]+/).filter(Boolean);
		const category = parts.length > 1 ? parts.shift() : "other";
		const label = titleWords(parts.length ? parts : file.replace(/\.sh$/, "").split(/[_-]+/).filter(Boolean));
		const key = category.toLowerCase();
		if (!groups.has(key)) groups.set(key, { label: key === "other" ? "Other" : titleWords([category]), items: [] });
		groups.get(key).items.push({ file, label });
	}
	return [...groups.values()]
		.map((group) => ({ ...group, items: group.items.sort((a, b) => a.label.localeCompare(b.label)) }))
		.sort((a, b) => (a.label === "Other") - (b.label === "Other") || a.label.localeCompare(b.label));
}

// Scripts in the dropdown: those without a button of their own, and not helpers, whose names start with _
function customScripts(site) {
	return site.scripts.filter((file) => !file.startsWith("_") && !hasScriptButton(site, file));
}
function scriptSelect(site, className) {
	const files = customScripts(site);
	const groups = customScriptGroups(files);
	const menu = el("div", { className: "script-menu " + (className || "") });
	const trigger = el("button", {
		type: "button", className: "small script-menu-trigger", ariaHasPopup: "menu", ariaExpanded: "false",
	}, svgIcon("play"), el("span", { textContent: "Run custom script" }), el("span", { className: "script-menu-chevron" }));
	trigger.lastChild.setAttribute("aria-hidden", "true");
	trigger.setAttribute("aria-haspopup", "menu");
	trigger.setAttribute("aria-expanded", "false");
	const panel = el("div", {
		className: "script-menu-panel", role: "menu", ariaLabel: "Custom site scripts", hidden: true,
	}, el("div", { className: "script-menu-header" },
		el("span", { className: "script-menu-eyebrow", textContent: "SITE AUTOMATIONS" }),
		el("strong", { textContent: "Custom scripts" }),
		el("span", { className: "script-menu-count", textContent: String(files.length) + (files.length === 1 ? " script" : " scripts") })));
	panel.setAttribute("role", "menu");
	panel.setAttribute("aria-label", "Custom site scripts");
	for (const group of groups) {
		const section = el("section", { className: "script-menu-group", role: "group", ariaLabel: group.label });
		section.setAttribute("role", "group");
		section.setAttribute("aria-label", group.label);
		section.append(el("h4", { textContent: group.label }));
		for (const { file, label } of group.items) {
			const item = el("button", {
				type: "button", className: "script-menu-item", role: "menuitem",
				onclick: (event) => {
					event.stopPropagation();
					panel.hidden = true;
					trigger.setAttribute("aria-expanded", "false");
					run(() => openTask({ kind: "script", site: site.name, script: file }));
				},
			}, el("span", { className: "script-menu-item-label", textContent: label }),
			el("code", { textContent: file }));
			item.dataset.script = file;
			item.setAttribute("role", "menuitem");
			section.append(item);
		}
		panel.append(section);
	}
	trigger.onclick = (event) => {
		event.stopPropagation();
		const open = panel.hidden;
		document.querySelectorAll(".script-menu-panel:not([hidden])").forEach((other) => { other.hidden = true; });
		document.querySelectorAll(".script-menu-trigger[aria-expanded=true]").forEach((other) => other.setAttribute("aria-expanded", "false"));
		panel.hidden = !open;
		trigger.setAttribute("aria-expanded", String(open));
		if (open) panel.querySelector('[role="menuitem"]')?.focus();
	};
	menu.append(trigger, panel);
	menu.addEventListener("click", (event) => event.stopPropagation());
	return menu;
}

document.addEventListener("click", (event) => {
	if (event.target.closest?.(".script-menu")) return;
	document.querySelectorAll(".script-menu-panel:not([hidden])").forEach((panel) => { panel.hidden = true; });
	document.querySelectorAll(".script-menu-trigger[aria-expanded=true]").forEach((trigger) => trigger.setAttribute("aria-expanded", "false"));
});
document.addEventListener("keydown", (event) => {
	const trigger = document.querySelector(".script-menu-trigger[aria-expanded=true]");
	if (event.key === "Escape") {
		if (!trigger) return;
		trigger.closest(".script-menu").querySelector(".script-menu-panel").hidden = true;
		trigger.setAttribute("aria-expanded", "false");
		trigger.focus();
		return;
	}
	const menuTrigger = event.target.closest?.(".script-menu-trigger");
	if (menuTrigger && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
		if (menuTrigger.getAttribute("aria-expanded") !== "true") menuTrigger.click();
		const items = [...menuTrigger.closest(".script-menu").querySelectorAll('[role="menuitem"]')];
		items[event.key === "ArrowUp" ? items.length - 1 : 0]?.focus();
		event.preventDefault();
		return;
	}
	const item = event.target.closest?.('[role="menuitem"]');
	if (!item || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
	const items = [...item.closest('[role="menu"]').querySelectorAll('[role="menuitem"]')];
	const index = items.indexOf(item);
	const next = event.key === "Home" ? 0
		: event.key === "End" ? items.length - 1
		: (index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
	items[next].focus();
	event.preventDefault();
});

function renderSite(name) {
	const openMenu = document.querySelector("#envs .script-menu-panel:not([hidden])");
	const activeMenuItem = document.activeElement?.closest?.(".script-menu-item");
	const openMenuState = openMenu ? {
		scrollTop: openMenu.scrollTop,
		focusedScript: activeMenuItem?.dataset.script || null,
	} : null;
	const site = allSites.find((s) => s.name === name);
	const head = $("#site-head");
	const envs = $("#envs");
	const packagesPanel = $("#site-packages");
	const targetsSection = $("#site-targets");
	const targets = $("#env-targets");
	const targetsActions = $("#site-targets-actions");
	const targetsAdd = $("#site-targets-add");
	targetsAdd.replaceChildren();
	head.replaceChildren();
	envs.replaceChildren();
	targets.replaceChildren();
	targetsActions.replaceChildren();
	targetsSection.hidden = true;
	$("#site-layout").classList.remove("with-packages");
	packagesPanel.replaceChildren();
	packagesPanel.hidden = true;
	$("#site-danger").hidden = true;
	if (!site && sitesLoaded) { go("#sites"); return; }
	if (!site) {
		head.append(el("p", { className: "empty", textContent: allSites.length ? "Site not found." : "Loading…" }));
		return;
	}
	const actions = el("div", { className: "actions" });
	const add = (label, handler, className) => actions.append(el("button", { textContent: label, className: className || "", onclick: () => run(handler) }));
	// Publish all, beside the title, publishes the main site and then every environment.
	// Publish environments, above the environments, publishes only them.
	let publishAll = null;
	if (site.has_project && hasProd(site) && site.scripts.includes("publish_prod.sh") && ["publish_local.sh", "publish.sh"].some((file) => site.scripts.includes(file))) {
		const deleted = site.environments.filter(isMissingEnv).map((env) => bindingLabel(env) || env.folder);
		const skipped = deleted.length ? `\nSkipped, as they no longer exist on their servers: ${deleted.join(", ")}.` : "";
		targetsActions.append(el("button", { textContent: "Publish environments", title: "Publish to every environment, not the main site", onclick: () => run(async () => {
			if (await ask({ title: "Publish environments?", message: `Build the main site and publish it to every environment, one at a time. The main site itself is not published. The services are restarted. If one fails, the rest are not published.${skipped}`, input: false, ok: "Publish environments" })) {
				await openTask({ kind: "publish_both", site: site.name, environments_only: true });
			}
		}) }));
		publishAll = el("button", { textContent: "Publish all", className: "detail-title-action", title: "Publish the main site, then every environment", onclick: () => run(async () => {
			if (await ask({ title: "Publish all?", message: `Publish the main site, then every environment, one at a time. The services are restarted. If one fails, the rest are not published.${skipped}`, input: false, ok: "Publish all" })) {
				await openTask({ kind: "publish_both", site: site.name });
			}
		}) });
	}
	if (site.has_project && site.kind !== "csharp" && site.kind !== "umbraco" && site.scripts.includes("outdated.sh")) {
		actions.append(el("button", { textContent: "Check outdated", title: "outdated.sh", onclick: () => run(() => openTask({ kind: "script", site: site.name, script: "outdated.sh" })) }));
	}
	if (site.kind === "python") {
		for (const [script, label] of [["update_packages.sh", "Update Python packages"]]) {
			if (site.scripts.includes(script)) {
				add(label, () => openTask({ kind: "script", site: site.name, script }));
			}
		}
	}
	// The main site's Sync settings… and Sync data…, beside Publish all: they open the same dialog
	const mainEnv = site.environments.find((env) => !isTarget(env));
	if (mainEnv && site.kind === "umbraco" && !site.admin) {
		if (dataSyncDestinations(site, mainEnv).length) {
			targetsActions.append(el("button", { textContent: "Sync data…", title: "Merge content and media from the main site into others", onclick: () => run(() => openSyncDialog(site, mainEnv, true)) }));
		}
		if (syncDestinations(site, mainEnv).length) {
			targetsActions.append(el("button", { textContent: "Sync settings…", title: "Sync settings from the main site", onclick: () => run(() => openSyncDialog(site, mainEnv)) }));
		}
	}
	const danger = $("#site-danger");
	danger.replaceChildren();
	danger.hidden = site.admin || !hasProd(site);
	if (!danger.hidden) danger.append(el("button", { textContent: "Delete everything", className: "danger", onclick: () => run(() => deleteSite(site, "all")) }));
	const title = el("div", { className: "detail-title" }, el("h2", { textContent: siteTitle(site) }), kindBadges(site));
	if (site.not_set_up && site.setup_kind !== "python") {
		title.append(el("button", { textContent: "Set up site…", className: "detail-title-action", onclick: () => run(() => openSetupSiteDialog(site)) }));
	}
	if ((site.adopt_into || []).length) {
		title.append(el("button", { textContent: "Move under main site", className: "detail-title-action", onclick: () => run(() => openAdoptDialog(site)) }));
	}
	if (publishAll) title.append(publishAll);
	head.append(title);
	if (actions.childElementCount) head.append(actions);
	for (const env of site.environments) {
		if (!isTarget(env)) envs.append(envPanel(site, env));
	}
	for (const env of sortedEnvironments(site.environments)) {
		if (!isTarget(env)) continue;
		const panel = envPanel(site, env);
		// What the filter matches: binding, aliases, environment, folder, server
		panel.dataset.search = [env.binding, bindingLabel(env), ...(env.aliases || []), ...aliasLabels(env), env.environment,
			env.folder, env.unit, env.path, env.host, env.target].filter(Boolean).join(" ").toLowerCase();
		targets.append(panel);
	}
	const canAddEnvironment = site.has_project && !site.admin && (site.kind === "csharp" || site.kind === "umbraco");
	const noEnvironments = !targets.childElementCount;
	if (canAddEnvironment && !noEnvironments) {
		targetsAdd.append(el("button", { textContent: "Add environment", onclick: () => run(() => openCreateProdDialog(site)) }));
	}
	$("#btn-empty-add-environment").onclick = () => run(() => openCreateProdDialog(site));
	$("#site-targets-head").hidden = noEnvironments;
	$("#site-targets-count").textContent = String(targets.childElementCount);
	// The filter is kept for the same site when the page is drawn again, and cleared for another site
	const search = $("#targets-search");
	if (search.dataset.site !== site.name) {
		search.value = "";
		search.dataset.site = site.name;
	}
	search.hidden = targets.childElementCount <= 2;
	if (search.hidden) search.value = "";
	filterTargets();
	$("#targets-empty").hidden = !(noEnvironments && canAddEnvironment);
	targetsSection.hidden = noEnvironments && !canAddEnvironment;
	if (site.has_project && (site.kind === "csharp" || site.kind === "umbraco")) {
		$("#site-layout").classList.add("with-packages");
		packagesPanel.hidden = false;
		const updates = new Map((site.package_updates || []).map((item) => [item.name.toLowerCase(), item]));
		const packages = (site.packages || []).filter((item) => item.name.toLowerCase() !== "microsoft.icu.icu4c.runtime" || updates.has(item.name.toLowerCase()));
		const packageActions = el("div", { className: "actions package-actions" });
		packageActions.append(el("button", { textContent: "Check", className: "small", title: "Check outdated packages", onclick: () => run(() => openTask({ kind: "bulk", sites: [site.name], action: "check" })) }));
		if (updates.size > 1) {
			packageActions.append(el("button", { textContent: "Update all", className: "small primary", onclick: () => run(() => updatePackages(site)) }));
		}
		packageActions.append(el("button", { textContent: "Add", className: "small package-add", title: "Add package", onclick: () => run(() => openPackageDialog(site, "add")) }),
			el("button", { textContent: "Remove", className: "small", title: "Remove package", onclick: () => run(() => openPackageDialog(site, "remove")) }));
		const packageList = el("div", { className: "package-list" });
		for (const item of packages) {
			const update = updates.get(item.name.toLowerCase());
			const nameButton = el("button", {
				type: "button", className: "package-copy", title: `Copy ${item.name}`,
				onclick: () => run(async () => {
					if (!navigator.clipboard) throw new Error("Clipboard access requires HTTPS or localhost.");
					await navigator.clipboard.writeText(item.name);
					toast(`Copied ${item.name}`, "ok");
				}),
			}, el("span", { className: "package-name" }, svgIcon("package"), el("span", { textContent: item.name })));
			const versions = el("div", { className: "package-versions" },
				el("span", { className: "package-version", textContent: update ? update.current : item.version || "Managed separately" }));
			if (update) versions.append(
				el("span", { className: "muted", textContent: "→", ariaLabel: "Update available to" }),
				el("span", { className: "package-version package-latest", textContent: update.latest }),
				el("button", { className: "small primary", textContent: "Update", title: `Update ${item.name} to ${update.latest}`,
					onclick: () => run(() => updatePackages(site, update)) }));
			// The whole row copies the name, except its own buttons
			packageList.append(el("div", { className: "package-row" + (update ? " has-update" : ""), title: `Copy ${item.name}`, onclick: (event) => {
				if (!event.target.closest("button")) nameButton.click();
			} }, nameButton, versions));
		}
		if (!packageList.children.length && !(site.package_errors || []).length) {
			packageList.append(el("p", { className: "muted", textContent: "No packages to display." }));
		}
		packagesPanel.append(
			el("div", { className: "package-head" },
				el("div", { className: "package-heading" }, el("h3", { textContent: "NuGet packages" }), el("span", { className: "package-count", textContent: String(packages.length), title: "Visible package references" }))),
			...(site.package_errors || []).map((message) => el("p", { className: "package-error", textContent: `Could not read package references: ${message}` })),
			...(site.updates === "error" ? [el("p", { className: "package-error", textContent: "Package update check failed. Run Check to retry." })] : []),
			...(site.updates === "available" && !updates.size ? [el("p", { className: "muted", textContent: "Run Check to load available package versions." })] : []),
			packageList,
			packageActions);
	}
	decorate(head);
	decorate(envs);
	decorate(targets);
	decorate(targetsActions);
	decorate(targetsAdd);
	decorate($("#site-danger"));
	updateSiteActivity();
	decorate(packagesPanel);
	if (openMenuState) {
		const panel = document.querySelector("#envs .script-menu-panel");
		const menu = panel?.closest(".script-menu");
		if (panel && menu) {
			panel.hidden = false;
			menu.querySelector(".script-menu-trigger").setAttribute("aria-expanded", "true");
			panel.scrollTop = openMenuState.scrollTop;
			const focusedItem = openMenuState.focusedScript
				&& [...panel.querySelectorAll(".script-menu-item")].find((item) => item.dataset.script === openMenuState.focusedScript);
			(focusedItem || menu.querySelector(".script-menu-trigger")).focus();
		}
	}
}

async function updatePackages(site, update) {
	const items = update ? [update] : site.package_updates;
	if (!(await ask({
		title: update ? "Update package" : "Update all packages",
		message: items.map((item) => `${item.name}: ${item.current} → ${item.latest}`).join("\n")
			+ "\nMajor upgrades may require code changes. This updates the project; publish separately.",
		input: false, ok: update ? "Update" : "Update all",
	}))) return;
	await openTask({ kind: "update_package", site: site.name, ...(update ? { package: update.name } : {}) });
}

// Scripts with dedicated controls are omitted from the custom scripts menu.
const buttonScripts = new Set(["publish.sh", "publish_local.sh", "publish_prod.sh", "create_prod.sh", "sync_prod.sh", "clear_logs.sh", "clear_logs_local.sh", "clear_logs_prod.sh", "issue_certificat.sh", "change_env.sh", "restart_service.sh", "check_service.sh", "edit_service.sh", "edit_conf.sh", "restart_apache.sh", "outdated.sh", "set_password.sh", "fix_permissions.sh", "update_packages.sh"]);
const projectScripts = new Set(["publish.sh", "publish_local.sh", "publish_prod.sh", "outdated.sh", "create_prod.sh"]);
const hasScriptButton = (site, file) => buttonScripts.has(file) && (site.has_project || !projectScripts.has(file));

// [script candidates or null, button label, confirmation text (null = none; the script asks itself), prod_action name]
function quickActions(env) {
	if (isTarget(env)) {
		return [
			[["publish_prod.sh"], "Publish", "Build the main site and publish it to this environment. The service is restarted."],
			[["clear_logs_prod.sh"], "Clear logs", "Delete the logs of this environment."],
			[null, "Issue certificate", "Issue a new certificate for this environment's binding.", "issue_certificate"],
			[null, "Change environment", null, "change_env"],
		];
	}
	return [
		[["publish_local.sh", "publish.sh"], "Publish", "Build and publish the test site. The service is restarted."],
		[["clear_logs_local.sh", "clear_logs.sh"], "Clear logs", "Delete the test site logs."],
		[null, "Issue certificate", "Issue a new certificate for the test site binding.", "issue_certificate"],
		[null, "Change environment", null, "change_env"],
	];
}

// A row of an environment card's buttons, named by its group at the left
function actionRow(group) {
	const row = el("div", { className: "actions quick" });
	row.dataset.group = group;
	return row;
}

function envPanel(site, env) {
	const missing = isMissingEnv(env);
	// IIS has no service to ask: keep-alive tells whether a Windows environment answered on its address
	const known = env.unit || (env.mode === "windows" && env.state);
	const dot = known ? env.state || "unknown" : "";
	const answered = env.mode === "windows" && env.state ? `Keep-alive ${formatDate(env.state_checked * 1000)}: ${env.state === "active" ? "answered" : env.state_error || "no answer"}` : "";
	const heading = el("div", { className: "env-head" },
		siteActivity(site, isTarget(env) ? "prod" : "main", isTarget(env) ? env.target || "prod" : ""),
		el("span", { className: "dot " + dot, title: answered }),
		el("span", { className: "badge " + (known ? "state-" + (env.state || "unknown") : ""), title: answered, textContent: known ? env.state || "unknown" : env.mode === "windows" ? "windows" : env.mode === "remote" ? "remote" : "static" }));
	const environment = site.kind === "python" ? "Live" : env.environment;
	if (environment) heading.append(el("span", { className: "badge env-" + environment.toLowerCase(), textContent: environment }));
	heading.append(httpsLock(env.https));
	if (isRemoteEnv(env)) heading.append(remoteIcon(env));
	if (missing) heading.append(el("span", { className: "badge env-missing", textContent: "Not found on server" }));
	const pending = publishBadge(env);
	if (pending && !missing) heading.append(el("div", { className: "publish-status" }, pending));

	const scheme = env.scheme || "https";
	const address = environmentAddress(env);
	const binding = !address.url ? bindingLink("") : site.admin && !isTarget(env)
		? el("span", { textContent: bindingLabel(env) })
		: openLink(bindingLabel(env) || address.hostname, address.url, address.title);
	// The main site's name is the page's title. Its binding, which can differ, is listed below.
	if (isTarget(env)) heading.prepend(el("h3", { className: "env-title", textContent: bindingLabel(env) || address.hostname }));
	const meta = el("dl", { className: "meta" }, el("dt", { textContent: "Binding" }), el("dd", {}, binding,
		...(site.answers_any_name && !isTarget(env) ? [el("span", { className: "badge any-name", title: "Apache sends a request for a name that no other site has to this site", textContent: "any name" })] : [])));
	if (env.aliases && env.aliases.length) {
		meta.append(el("dt", { textContent: "Aliases" }), el("dd", { className: "database-name aliases" },
			...env.aliases.flatMap((alias, index) => [
				...(index ? [", "] : []),
				openLink(aliasLabels(env)[index], scheme + "://" + alias + "/"),
			])));
	}
	if (site.kind === "umbraco" && address.url) meta.append(el("dt", { textContent: "Backoffice" }), el("dd", {}, openLink((bindingLabel(env) || address.hostname) + "/umbraco", address.url + "umbraco", address.title)));
	if (site.kind === "csharp" || site.kind === "umbraco") {
		meta.append(el("dt", { textContent: "Datasource" }), el("dd", {
			className: "database-name",
			textContent: (env.database || "Unknown") + (env.database_server ? ` (${env.database_server.replace(/^\.(?=$|[\\,])/, "localhost")})` : ""),
			title: "Inferred from database configuration; connection and server edition are not tested.",
		}));
		if (env.database_name) meta.append(el("dt", { textContent: "Database" }), el("dd", {
			className: "database-name", textContent: env.database_name,
		}));
	}
	if (env.dotnet) meta.append(el("dt", { textContent: "Runtime" }), el("dd", { textContent: dotnetLabel(env.dotnet), title: `This environment runs on .NET ${env.dotnet}, read from its runtimeconfig.json` }));
	if (env.port) meta.append(el("dt", { textContent: "Port" }), el("dd", { textContent: env.port }));
	if (env.mode === "remote") {
		meta.append(el("dt", { textContent: "Server" }), el("dd", { textContent: `${env.user || "?"}@${env.host || "?"}:${env.ssh_port || 22}` }));
		meta.append(el("dt", { textContent: "Folder" }), el("dd", { textContent: env.folder ? "/var/www/" + env.folder : "—" }));
	} else if (env.mode === "windows") {
		meta.append(el("dt", { textContent: "Server" }), el("dd", { textContent: `${env.user || "?"}@${env.host || "?"} (Windows)` }));
		meta.append(el("dt", { textContent: "Folder" }), el("dd", { textContent: env.folder || "—" }));
	} else {
		meta.append(el("dt", { textContent: "Folder" }), el("dd", { textContent: "/var/www/" + env.path }));
		if (env.unit) meta.append(el("dt", { textContent: "Service" }), el("dd", { textContent: env.unit }));
	}

	const actions = el("div", { className: "actions" });
	actions.dataset.group = "Site";
	const add = (label, handler, className, row = actions) => row.append(el("button", { textContent: label, className: "small " + (className || ""), onclick: () => run(handler) }));
	// Migrate, Copy and Delete change where the site lives, so they have their own row
	const manage = el("div", { className: "actions" });
	manage.dataset.group = "Manage";
	const addManage = (label, handler, className) => add(label, handler, className, manage);
	if ((env.unit || env.mode === "remote") && !missing) {
		if (env.unit !== site.name || !site.admin) add(env.state === "active" ? "Stop" : "Start", () => serviceAction(site, env, env.state === "active" ? "stop" : "start"));
		add("Restart", () => serviceAction(site, env, "restart"));
		add("Logs", () => showLogs(site, env));
	}
	if (missing) {
		// Only Publish and Delete site are offered: there is nothing on the server to work with
	} else if (env.mode === "windows") {
		// No shell on IIS, but its files can be browsed and edited over SMB
		add("Files", () => openFiles("", remoteRef(site, env)), "desktop-only");
	} else if (env.mode === "remote") {
		add("Shell", () => openTask({ kind: "remote_tool", site: site.name, tool: "shell", ...targetFields(env) }), "desktop-only");
		add("Files", () => openFiles("", remoteRef(site, env)), "desktop-only");
	} else if (env.mode !== "windows") {
		add("Shell", () => openTask({ kind: "shell", site: site.name, path: env.path, scope: isTarget(env) ? "prod" : "main" }), "desktop-only");
		add("Files", () => openFiles(env.path), "desktop-only");
	}

	// Each row is one kind of task, named at its left
	const quick = actionRow("Deploy");
	const sync = actionRow("Sync");
	const settings = actionRow("Setup");
	settings.setAttribute("role", "group");
	settings.setAttribute("aria-label", "Environment settings");
	if (!site.admin && env.mode !== "windows" && !missing) {
		settings.append(el("button", { textContent: "Fix permissions", className: "small", onclick: () => run(async () => {
			if (await ask({ title: "Fix permissions?", message: "Restore www-data ownership and content access for this environment. Scripts and symbolic links are skipped. No service restart is needed.", input: false, ok: "Fix permissions" })) {
				await openTask({ kind: "fix_permissions", site: site.name, scope: isTarget(env) ? "prod" : "test", ...targetFields(env) });
			}
		}) }));
	}
	if ((!hasProd(site) || !site.admin) && env.mode !== "windows" && !missing) {
		settings.append(el("button", { textContent: "Change bindings", className: "small desktop-only", onclick: () => run(() => changeBinding(site, isTarget(env) ? "prod" : "test", env)) }));
	}
	let extraTools = null;
	for (const [files, label, confirm, action] of quickActions(env)) {
		if (label === "Publish" && !site.has_project) continue;
		if (missing && label !== "Publish") continue;
		let file = files && files.find((f) => (isTarget(env) ? env.scripts || [] : site.scripts).includes(f));
		if (!file && label === "Publish" && !isTarget(env)) file = site.scripts.find((f) => /^publish.*\.sh$/.test(f) && f !== "publish_prod.sh");
		if (!action && !file) continue;
		if (action && env.mode === "remote" && !env.host) continue;
		if (action === "issue_certificate" && env.mode === "windows") continue;
		if (action && !isTarget(env) && (!env.unit || site.admin) && action === "change_env") continue;
		(action ? settings : quick).append(el("button", { textContent: label, className: "small" + (action === "change_env" ? " desktop-only" : ""), title: file || action, onclick: () => run(async () => {
			if (action === "change_env") { openEnvironmentDialog(site, env); return; }
			const confirmText = missing && label === "Publish"
				? `The site no longer exists on ${env.host}. This creates it again as a new installation, without its old content and database.`
				: confirm;
			if (confirmText && !(await ask({ title: `${label}?`, message: `${confirmText} (${bindingLabel(env) || siteLabel(site)})`, input: false, ok: label }))) return;
			await openTask(action ? { kind: "prod_action", site: site.name, action, scope: isTarget(env) ? "prod" : "test", ...targetFields(env) } : { kind: "script", site: site.name, script: file, ...targetFields(env) });
		}) }));
	}
	// Every site with somewhere to sync to has the button, also the main site with a single environment
	if (!missing && syncDestinations(site, env).length > 0) {
		sync.append(el("button", { textContent: "Sync settings…", className: "small", title: "Sync uSync settings, Views, css and scripts from this site", onclick: () => run(() => openSyncDialog(site, env)) }));
	}
	if (site.kind === "umbraco" && !missing && !site.admin) {
		if (dataSyncDestinations(site, env).length) {
			// Sync data… always comes before Sync settings…
			const dataButton = el("button", { textContent: "Sync data…", className: "small", title: "Merge content and media from this site into another", onclick: () => run(() => openSyncDialog(site, env, true)) });
			sync.insertBefore(dataButton, [...sync.children].find((button) => button.textContent === "Sync settings…") || null);
		}
		// Setup first, at the left: the syncs it allows stand at the right
		sync.prepend(el("button", { textContent: "Sync setup…", className: "small", title: dataSyncSummary(env), onclick: () => run(() => openDataSyncDialog(site, env)) }));
	}
	if (!isTarget(env) && customScripts(site).length) quick.append(scriptSelect(site));
	if (env.mode === "windows") {
		// web.config is the IIS site's config, appsettings.json its application settings
		const tools = actionRow("Config");
		for (const [label, filename] of [["Edit web.config", "web.config"], ["Edit appsettings.json", "appsettings.json"]]) {
			tools.append(el("button", { textContent: label, className: "small desktop-only", onclick: () => run(() => editFile(filename, remoteRef(site, env))) }));
		}
		extraTools = tools;
	}
	if ((env.unit || env.mode === "remote" || env.apache) && !missing) {
		const tools = actionRow("Config");
		for (const [tool, label] of [["check_service", "Check service"], ["edit_service", "Edit service"], ["edit_conf", "Edit config"]]) {
			if (tool !== "edit_conf" && !env.unit && env.mode !== "remote") continue;
			tools.append(el("button", { textContent: label, className: "small" + (tool === "check_service" ? "" : " desktop-only"), onclick: () => run(() => tool === "check_service"
				? openTask({ kind: env.mode === "remote" ? "remote_tool" : "unit_tool", site: site.name, unit: env.unit, tool, scope: isTarget(env) ? "prod" : "main", ...targetFields(env) })
				: editConfiguration(site, env, tool === "edit_service" ? "service" : "conf")) }));
		}
		if (site.kind === "csharp" || site.kind === "umbraco") {
			for (const filename of ["appsettings.json"]) {
				if (env.mode !== "remote" && !(env.editable_configs || []).includes(filename)) continue;
				tools.append(el("button", { textContent: "Edit " + filename, className: "small desktop-only", onclick: () => run(() =>
					editFile(env.mode === "remote" ? filename : join(env.publish_path, filename), env.mode === "remote" ? remoteRef(site, env) : "")) }));
			}
		}
		extraTools = tools;
	}
	if (isTarget(env) && (env.mode === "local" && env.exists !== false || env.mode === "remote" && !missing)) addManage("Migrate…", () => openCreateProdDialog(site, env));
	if (isTarget(env) && (env.mode === "local" && env.exists !== false || env.mode === "windows" || env.mode === "remote" && !missing)) addManage("Copy site", () => openCreateProdDialog(site, env, true));
	// An environment on Windows is only removed here: its IIS site and files stay on the server
	if (hasProd(site) && !site.admin && env.mode === "windows") addManage("Remove environment", () => deleteSite(site, "prod", env), "push-right");
	else if (hasProd(site) && !site.admin) addManage("Delete site", () => deleteSite(site, isTarget(env) ? "prod" : "test", env), "danger");
	if (!hasProd(site) && !site.admin) {
		if (!site.managed_by) addManage("Copy site", () => copySite(site));
		addManage("Delete site", () => deleteSite(site, "all"), "danger");
	}

	const panel = el("section", { className: "env " + (isTarget(env) ? "prod" : "") }, heading, meta);
	if (missing) panel.append(el("p", { className: "package-error", textContent: `The site no longer exists on ${env.host}. Publish creates it again. Delete site removes this environment here.` }));
	else if (env.exists === false) panel.append(el("p", { className: "muted", textContent: "Nothing published here yet." }));
	if (env.remote_error) panel.append(el("p", { className: "package-error", textContent: "Remote status unavailable: " + env.remote_error }));
	if (quick.children.length) panel.append(quick);
	if (sync.children.length) panel.append(sync);
	if (settings.children.length) panel.append(settings);
	if (extraTools) panel.append(extraTools);
	if (actions.children.length) panel.append(actions);
	if (manage.children.length) panel.append(manage);
	// Tools that are rarely the point of the row stand to the right: clearing logs, editing files, the syncs,
	// and logs, shell and files beside Stop and Restart. Only with something to their left, so a row of edit
	// buttons alone (on Windows) stays at the left.
	for (const row of [quick, sync, extraTools, actions]) {
		const first = row && [...row.children].find((button) => /^(Clear logs|Sync settings…|Sync data…|Edit .*|Logs|Shell|Files)$/.test(button.textContent));
		if (first && first !== row.firstElementChild) first.classList.add("push-right");
	}
	return panel;
}

async function serviceAction(site, env, action) {
	const unit = env.unit || env.folder;
	if (action !== "start" && !(await ask({ title: `${action === "stop" ? "Stop" : "Restart"} ${bindingLabel(env) || siteLabel(site)}?`, message: bindingLabel(env) || siteLabel(site), input: false, ok: action === "stop" ? "Stop" : "Restart", danger: action === "stop" }))) return;
	await api(`/api/sites/${encodeURIComponent(site.name)}/service`, { json: { action, unit: env.unit, scope: env.mode === "remote" ? "prod" : "local", ...targetFields(env) } });
	toast(`${unit}: ${action} done`, "ok");
	await loadSites();
}

const logsDialog = $("#dlg-logs");
// Another log starts with every line shown, as a list
function resetLogView() {
	logLevel = "all";
	$("#logs-search").value = "";
	$("#logs-raw").setAttribute("aria-pressed", "false");
	$("#logs-raw-text").hidden = true;
	$("#logs-text").hidden = false;
}
let logsTarget = null;
async function showLogs(site, env) {
	logsTarget = { site, env };
	resetLogView();
	$("#logs-title").textContent = `Logs · ${env.unit || env.folder}`;
	if (!logsDialog.open) logsDialog.showModal();
	await refreshLogs();
}
// Log lines, read into time, level, source and message, so they read like a list and not a terminal.
// Apache's error log, the journal (site and admin services) and anything else, line by line.
let logEntries = [];
let logLevel = "all";
const logLevelOf = (text) => /\b(crit|alert|emerg|fatal|fail(ed|ure)?|error|err|exception|panic|denied)\b/i.test(text) ? "error"
	: /\bwarn(ing)?\b/i.test(text) ? "warn" : "info";
function parseLog(text) {
	const entries = [];
	for (const line of String(text || "").split("\n")) {
		if (!line.trim()) continue;
		// tail marks where each file starts when it reads several
		const file = line.match(/^==> (.+) <==$/);
		if (file) {
			entries.push({ file: file[1], level: "file", time: "", source: "", message: "" });
			continue;
		}
		// A stack trace or a wrapped line belongs to the line above
		if (entries.length && /^(\s+|at |--- )/.test(line)) {
			entries[entries.length - 1].message += "\n" + line;
			continue;
		}
		let match = line.match(/^\[([^\]]+)\]\s+\[(?:([^\]:]+):)?([a-z0-9]+)\]\s+(?:\[pid [^\]]*\]\s+)?(?:\[client [^\]]*\]\s+)?(.*)$/i);
		if (match) {
			const time = match[1].replace(/^\w{3}\s+/, "").replace(/\.\d+/, "").replace(/\s+\d{4}$/, "");
			const level = /^(error|crit|alert|emerg)$/i.test(match[3]) ? "error" : /^warn/i.test(match[3]) ? "warn" : "info";
			entries.push({ time, source: match[2] || "", level, message: match[4] });
			continue;
		}
		match = line.match(/^([A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d:\d\d|\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)\S*\s+\S+\s+([^\s:[]+)(?:\[\d+\])?:\s?(.*)$/);
		if (match) {
			// The journal writes each line of a stack trace on its own, with the same prefix
			const previous = entries[entries.length - 1];
			if (previous && previous.source === match[2] && /^(\s+|at |--- )/.test(match[3])) {
				previous.message += "\n" + match[3];
				continue;
			}
			// .NET starts a line with its level (info:, warn:, fail:), which the level shows already
			entries.push({ time: match[1].replace("T", " "), source: match[2], level: logLevelOf(match[3]),
				message: match[3].replace(/^(trce|dbug|info|warn|fail|crit):\s*/, "") });
			continue;
		}
		entries.push({ time: "", source: "", level: logLevelOf(line), message: line });
	}
	return entries;
}
function renderLogs() {
	const words = $("#logs-search").value.trim().toLowerCase();
	const counts = { all: logEntries.filter((entry) => !entry.file).length, error: 0, warn: 0 };
	for (const entry of logEntries) if (entry.level in counts) counts[entry.level] += 1;
	// Only the levels the log has: a level that is gone shows every line again
	if (logLevel !== "all" && !counts[logLevel]) logLevel = "all";
	for (const button of document.querySelectorAll(".logs-levels button")) {
		button.hidden = button.dataset.level !== "all" && !counts[button.dataset.level];
		button.classList.toggle("active", button.dataset.level === logLevel);
		button.querySelector("span").textContent = counts[button.dataset.level] || "";
	}
	const matches = logEntries.filter((entry) => entry.file || ((logLevel === "all" || entry.level === logLevel)
		&& (!words || `${entry.source} ${entry.message}`.toLowerCase().includes(words))));
	// A file is only shown with lines under it: an empty log, or one the filter leaves nothing of, has no group
	const shown = matches.filter((entry, index) => !entry.file || (matches[index + 1] && !matches[index + 1].file));
	const list = $("#logs-text");
	list.replaceChildren(...shown.map((entry) => entry.file ? el("div", { className: "log-file" },
		el("span", { textContent: entry.file, title: entry.file }),
		el("button", { type: "button", className: "log-file-clear", textContent: "Clear", title: "Empty this log file", onclick: () => run(() => clearLogFile(entry.file)) }))
		: el("div", { className: "log-row level-" + entry.level },
		el("span", { className: "log-time", textContent: entry.time }),
		el("span", { className: "log-level", textContent: { error: "Error", warn: "Warning", info: "Info" }[entry.level] }),
		el("span", { className: "log-source", textContent: entry.source, title: entry.source }),
		el("span", { className: "log-message", textContent: entry.message }))));
	if (!shown.length) list.append(el("p", { className: "muted logs-empty", textContent: logEntries.length ? "No lines match." : "No log lines." }));
	const lineCount = logEntries.filter((entry) => !entry.file).length;
	$("#logs-count").textContent = `${shown.filter((entry) => !entry.file).length} of ${lineCount} lines`;
	list.scrollTop = list.scrollHeight;
}
$("#logs-search").oninput = renderLogs;
for (const button of document.querySelectorAll(".logs-levels button")) button.onclick = () => { logLevel = button.dataset.level; renderLogs(); };
$("#logs-raw").onclick = () => {
	const raw = $("#logs-raw").getAttribute("aria-pressed") !== "true";
	$("#logs-raw").setAttribute("aria-pressed", String(raw));
	$("#logs-raw-text").hidden = !raw;
	$("#logs-text").hidden = raw;
	if (raw) $("#logs-raw-text").scrollTop = $("#logs-raw-text").scrollHeight;
};
async function clearLogFile(path) {
	if (!await ask({ title: "Clear log file?", message: `${path} is emptied. Its lines cannot be brought back.`, input: false, ok: "Clear", danger: true })) return;
	await api("/api/server-logs/clear", { method: "POST", json: { path } });
	toast("Log file cleared", "ok");
	await refreshLogs();
}
async function refreshLogs() {
	$("#logs-text").replaceChildren(el("p", { className: "muted logs-empty", textContent: "Loading…" }));
	let data;
	try {
		data = await api(logsTarget.source ? `/api/server-logs?source=${logsTarget.source}`
			: `/api/sites/${encodeURIComponent(logsTarget.site.name)}/logs?unit=${encodeURIComponent(logsTarget.env.unit)}&scope=${logsTarget.env.mode === "remote" ? "prod" : "local"}${logsTarget.env.target && logsTarget.env.target !== "prod" ? "&target=" + encodeURIComponent(logsTarget.env.target) : ""}`);
	} catch (error) {
		$("#logs-text").replaceChildren(el("p", { className: "logs-error", textContent: error.message }));
		throw error;
	}
	$("#logs-raw-text").textContent = data.text || "(no log lines)";
	// Where the lines come from: the files, or the journal command for a service
	logEntries = parseLog(data.text);
	// Files are named in the list, above their lines; a journal has no file, so its command is named under the title
	$("#logs-source").textContent = (data.sources || []).join(" · ");
	$("#logs-source").hidden = !(data.sources || []).length || logEntries.some((entry) => entry.file);
	renderLogs();
}
$("#logs-refresh").onclick = () => run(refreshLogs);
$("#logs-close").onclick = () => logsDialog.close();
for (const source of ["admin", "apache"]) {
	$(`#settings-${source}-logs`).onclick = () => run(async () => {
		logsTarget = { source };
		resetLogView();
		$("#logs-title").textContent = source === "admin" ? "Admin app log" : "Apache error logs";
		if (!logsDialog.open) logsDialog.showModal();
		await refreshLogs();
	});
}

async function changeBinding(site, scope, env) {
	const current = env ? bindingLabel(env) : site.binding;
	const label = scope === "prod" ? "production site" : "test site";
	const bindings = await ask({
		title: `Change ${label} bindings`,
		placeholder: scope === "prod" ? "" : "Empty: answers any name",
		message: `Set the primary domain and aliases for the ${label} of ${siteLabel(site)}. Aliases must resolve to the same server. HTTP-only sites remain HTTP-only; certificates are updated only for HTTPS sites. Only an HTTP-only site can use a name without a dot, such as the server's own name on the local network.`,
		value: current,
		extra: { label: "Aliases (one per line)", value: aliasLabels(env).join("\n"), placeholder: "www.example.com\napi.example.com" },
		ok: "Change",
	});
	if (!bindings) return;
	// An empty binding makes the site answer any name, with the server's own name as its binding
	const binding = bindings.value.trim() || (scope === "prod" ? "" : "*");
	const aliases = bindings.extra.trim();
	const currentAliases = aliasLabels(env).join(" ").toLowerCase();
	if (binding !== current || aliases.replace(/,/g, " ").trim().replace(/\s+/g, " ").toLowerCase() !== currentAliases) {
		await openTask({ kind: "change_binding", site: site.name, binding, aliases, scope, ...targetFields(scope === "prod" ? env : null) });
	}
}

async function copySite(site) {
	const dialog = $("#dlg-copy-site");
	const form = $("#form-copy-site");
	form.reset();
	$("#copy-site-icon").replaceChildren(svgIcon("file"));
	const field = (name) => form.elements.namedItem(name);
	const hasSqlConnection = site.environments.some((env) =>
		!isTarget(env) && ["MS SQL", "SQL Express", "Azure SQL"].includes(env.database));
	field("change_connection").closest("label").hidden = !hasSqlConnection;
	field("change_connection").disabled = !hasSqlConnection;
	const sqlFields = $("#copy-sql-fields");
	sqlFields.hidden = true;
	sqlFields.disabled = true;
	let databaseEdited = false;
	let bindingEdited = false;
	let usernameEdited = false;
	const suggestUsername = () => {
		if (!usernameEdited) field("username").value = field("database").value;
	};
	const showCopyPath = () => {
		$("#copy-site-path").textContent = `Copied to /var/www/${field("target").value.trim() || "…"}`;
	};
	showCopyPath();
	// A site with a project gets a project of its own
	const hasProject = site.has_project;
	$("#copy-project-row").hidden = !hasProject;
	field("project").required = hasProject;
	field("project").disabled = !hasProject;
	let projectEdited = false;
	field("project").oninput = () => { projectEdited = true; };
	field("target").oninput = () => {
		showCopyPath();
		if (hasProject && !projectEdited) field("project").value = field("target").value.trim().replace(/[^A-Za-z0-9._-]/g, "").replace(/^[._-]+/, "");
		if (!databaseEdited) {
			field("database").value = field("target").value;
			suggestUsername();
		}
		if (!bindingEdited) field("binding").value = field("target").value;
	};
	field("database").oninput = () => {
		databaseEdited = true;
		suggestUsername();
	};
	field("username").oninput = () => { usernameEdited = true; };
	field("binding").oninput = () => { bindingEdited = true; };
	field("change_connection").onchange = () => {
		sqlFields.hidden = !field("change_connection").checked;
		sqlFields.disabled = !field("change_connection").checked;
	};
	$("#copy-site-cancel").onclick = () => dialog.close();
	dialog.onclose = () => form.reset();
	form.onsubmit = (event) => {
		event.preventDefault();
		run(async () => {
			const submit = form.querySelector('[type="submit"]');
			submit.disabled = true;
			try {
				const payload = { kind: "copy_site", site: site.name,
					target: field("target").value.trim(), binding: field("binding").value.trim() };
				if (hasProject) payload.project = field("project").value.trim();
				if (hasSqlConnection && field("change_connection").checked) {
					payload.connection = { server: field("server").value.trim(), database: field("database").value.trim(),
						username: field("username").value.trim(), password: field("password").value };
				}
				await openTask(payload);
				dialog.close();
			} finally {
				submit.disabled = false;
			}
		});
	};
	dialog.showModal();
}

async function deleteSite(site, scope, selected) {
	const allTargets = site.environments.filter(isTarget);
	const prod = scope === "prod" && selected ? selected : allTargets[0];
	const main = site.environments.find((e) => e.key === "main");
	const mainEnvironment = environmentLabel(site, main);
	const prodEnvironment = prod ? environmentLabel(site, prod) : "";
	const remote = prod && prod.mode === "remote";
	const windows = prod && prod.mode === "windows";
	const windowsKept = `The IIS site and its files on ${prod?.host} are not touched; remove them on the server yourself.`;
	const describeTarget = (env) => {
		const place = env.mode === "windows" ? `${env.host} (Windows: only removed here, delete the IIS site and its files yourself)`
			: env.mode === "remote" ? `${env.host} (files, service and Apache config)` : "this server (files, service and Apache config)";
		return `• ${environmentLabel(site, env)}, ${bindingLabel(env) || env.folder || ""} on ${place}`;
	};
	const target = scope === "prod" ? (bindingLabel(prod) || prod.unit || prod.folder || siteLabel(site)) : siteLabel(site);
	const what = {
		all: allTargets.length > 1
			? `This deletes the site and all its environments:\n${allTargets.map(describeTarget).join("\n")}`
			: remote
			? `This deletes the site and its remote ${prodEnvironment} site on ${prod.host}: files, services, Apache configs and logs on both servers.`
			: windows
				? `This deletes the site: its files, service, Apache config and logs. ${windowsKept}`
			: prod
				? `This deletes the site and its ${prodEnvironment} site on this server: files, services, Apache configs and logs.`
				: `This deletes the site: its files, service, Apache config and logs.`,
		test: `This deletes the ${mainEnvironment} site's files, service and Apache config. The ${prodEnvironment} site is kept.`,
		prod: remote
			? `This deletes the ${prodEnvironment} site on ${prod.host}: its files, service, Apache config and logs. The ${mainEnvironment} site is kept.`
			: windows
				? `This removes the ${prodEnvironment} target from this site. ${windowsKept} The ${mainEnvironment} site is kept.`
			: `This deletes the ${prodEnvironment} site: its files, service, Apache config and logs. The ${mainEnvironment} site is kept.`,
	}[scope];
	// Removing a Windows environment deletes nothing on the server, so it is a plain confirmation
	const typed = scope === "prod" && windows
		? await ask({ title: "Remove environment?", message: `${what}\nPublish, sync and the other tools for ${target} are removed here.`, input: false, ok: "Remove" })
		: await ask({
			title: scope === "all" ? "Delete everything" : `Delete ${scope === "prod" ? prodEnvironment : mainEnvironment} site`, danger: true, ok: "Delete", match: target,
			message: `${what}\nType "${target}" to confirm.`,
		});
	if (typed === null) return;
	let certificates = false;
	const certificateMessage = remote && scope !== "test"
		? "Also revoke and remove the certificate(s) for the site being deleted? The remote certificate is kept if another enabled site uses the domain."
		: "Also revoke and remove the certificate(s) for the site being deleted?";
	const deletedEnvironments = site.environments.filter((env) => scope === "all" || (scope === "prod" ? env === prod : !isTarget(env)));
	if (deletedEnvironments.some((env) => env.https !== false && env.mode !== "windows")) {
		certificates = (await ask({ title: "Certificate", message: certificateMessage, input: false, ok: "Remove certificate(s)" })) === true;
	}
	await openTask({ kind: "delete_site", site: site.name, certificates, scope, ...(scope === "prod" ? { target: prod.target || "prod" } : {}) });
}

$("#search").oninput = (event) => {
	query = event.target.value;
	if (currentView !== "sites") go("sites");
	renderSites();
};
$("#btn-server-shell").onclick = () => run(() => openTask({ kind: "shell" }));
$("#btn-browse").onclick = () => run(() => openFiles("", "", true));
$("#btn-check-updates").onclick = () => run(async () => {
	// It asks NuGet about every project on the server, which takes a while: only on purpose
	const projects = listedSites().filter((site) => site.has_project).length;
	if (!await ask({ title: "Check for updates?", message: `Check NuGet for package updates in all ${projects} projects on this server. It can take a few minutes. Nothing is installed.`, input: false, ok: "Check for updates" })) return;
	$("#btn-check-updates").disabled = true;
	try {
		updateCheckCurrent.clear();
		updateCheckFeedback = true;
		monitorUpdateCheck(await api("/api/package-updates", { method: "POST" }));
	} catch (error) {
		$("#btn-check-updates").disabled = false;
		throw error;
	}
});
document.addEventListener("keydown", (event) => {
	const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
	if (event.key === "/" && !typing && !$("#search").hidden && currentView !== "sql") { event.preventDefault(); if (currentView !== "sites") go("sites"); $("#search").focus(); }
	if (event.key === "`" && event.ctrlKey) { event.preventDefault(); toggleDock(); }
	if (event.key === "Escape") $("#user-menu").hidden = true;
});
setInterval(() => {
	const busy = document.activeElement && $("#view-sites").contains(document.activeElement) && document.activeElement.tagName !== "BODY";
	if (!document.hidden && currentView !== "sql" && !busy && !document.querySelector("dialog[open]")) loadSites().catch(() => {});
}, 15000);

// ------------------------------------------------------------------ account

$("#btn-user").onclick = (event) => { event.stopPropagation(); $("#settings-menu").hidden = true; $("#user-menu").hidden = !$("#user-menu").hidden; };
document.addEventListener("click", (event) => { if (!$("#user-menu").contains(event.target)) $("#user-menu").hidden = true; });
const profileDialog = $("#dlg-profile");
const profileForm = $("#form-profile");
$("#menu-profile").onclick = () => {
	$("#user-menu").hidden = true;
	profileForm.reset();
	profileForm.username.value = $("#menu-user").textContent;
	profileDialog.showModal();
	profileForm.current_password.focus();
};
$("#profile-cancel").onclick = () => profileDialog.close();
profileForm.onsubmit = (event) => {
	event.preventDefault();
	const data = Object.fromEntries(new FormData(profileForm));
	run(async () => {
		if (data.new_password !== data.repeat) throw new Error("The new passwords do not match.");
		delete data.repeat;
		const result = await api("/api/profile", { json: data });
		$("#menu-user").textContent = result.user;
		$("#btn-user").textContent = result.user.slice(0, 1).toUpperCase();
		profileDialog.close();
		toast("Profile saved", "ok");
	});
};

const dialog = $("#dlg-site");
const siteForm = $("#form-site");
let templateRequest = null;
let dotnetTemplates = [];
let templatesLoaded = false;
function syncTemplateVersion() {
	siteForm.elements.template.setCustomValidity("");
	const name = siteForm.elements.template.value.trim();
	const umbraco = name.toLowerCase() === "umbraco";
	const template = dotnetTemplates.find((item) => item.short_name === name);
	siteForm.version.placeholder = umbraco ? defaultUmbracoVersion : template?.installed_version || "Installed template version";
	if (!templateRequest) $("#template-status").textContent = umbraco
		? "Umbraco includes uSync and VNS.Umbraco."
		: "Default: web. Version applies to NuGet templates.";
}
async function loadDotnetTemplates() {
	if (templateRequest) return templateRequest;
	$("#template-status").textContent = "Loading installed templates…";
	templateRequest = (async () => {
		let loaded = false;
		try {
			const data = await api("/api/dotnet/templates");
			dotnetTemplates = data.templates;
			templatesLoaded = true;
			$("#dotnet-templates").replaceChildren(...data.templates.map((template) =>
				el("option", { value: template.short_name, label: `${template.name} · ${template.language} · ${template.tags}` })));
			loaded = true;
		} catch (error) {
			templatesLoaded = false;
			$("#template-status").textContent = "Could not load templates. Enter a short name (default: web).";
			throw error;
		} finally {
			templateRequest = null;
			if (loaded) syncTemplateVersion();
		}
	})();
	return templateRequest;
}
function syncSiteForm() {
	const csharp = siteForm.type.value === "csharp";
	$("#csharp-options").hidden = !csharp;
	$("#template-name-option").hidden = !csharp;
	$("#template-status").hidden = !csharp;
	siteForm.version.disabled = !csharp;
	siteForm.elements.template.disabled = !csharp;
	syncTemplateVersion();
	if (csharp) run(loadDotnetTemplates);
}
siteForm.addEventListener("change", (event) => { if (event.target.name === "type") syncSiteForm(); });
// Choosing a kind with the mouse moves on to the folder name. Arrow keys between the kinds keep the focus there.
siteForm.addEventListener("click", (event) => {
	if (event.detail > 0 && event.target.closest(".type-card")) setTimeout(() => siteForm.site.focus());
});
siteForm.elements.template.oninput = syncTemplateVersion;
siteForm.site.oninput = () => { if (!siteForm.binding.dataset.edited) siteForm.binding.value = siteForm.site.value; };
siteForm.binding.oninput = () => { siteForm.binding.dataset.edited = "1"; };
// The binding's message is about the certificate choice as well: any change clears it, or the browser keeps
// refusing the form before it is checked again
for (const type of ["input", "change"]) siteForm.addEventListener(type, () => siteForm.binding.setCustomValidity(""));
$("#btn-new-site").onclick = () => { siteForm.reset(); siteForm.elements.template.setCustomValidity(""); siteForm.binding.setCustomValidity(""); delete siteForm.binding.dataset.edited; syncSiteForm(); dialog.showModal(); siteForm.site.focus(); };
$("#btn-empty-new-site").onclick = () => $("#btn-new-site").click();
$("#site-cancel").onclick = (e) => { e.preventDefault(); dialog.close(); };
siteForm.onsubmit = (event) => {
	event.preventDefault();
	run(async () => {
		const field = siteForm.elements.template;
		if (siteForm.type.value === "csharp") {
			if (templateRequest) await templateRequest;
			else if (!templatesLoaded) await loadDotnetTemplates();
			const name = field.value.trim();
			const match = dotnetTemplates.find((item) => item.short_name.toLowerCase() === (name || "web").toLowerCase());
			if (!match && name.toLowerCase() !== "umbraco") {
				field.setCustomValidity("Choose an installed web template by its exact short name.");
				field.reportValidity();
				return;
			}
			field.value = name ? match?.short_name || "umbraco" : "";
		}
		const data = Object.fromEntries(new FormData(siteForm));
		// No binding: the site answers any name no other site has, with this server's own name as its binding
		data.binding = data.binding.trim() || "*";
		if (data.type === "csharp" && data.template?.toLowerCase() === "umbraco" && !data.version?.trim()) {
			data.version = defaultUmbracoVersion;
		}
		data.kind = "create_site";
		await openTask(data);
		dialog.close();
	});
};

const prodDialog = $("#dlg-prod");
const prodForm = $("#form-prod");
function syncProdForm() {
	const target = prodForm.elements.target.value;
	const local = target === "local";
	const windows = target === "windows";
	const linuxRemote = target === "remote";
	$("#prod-local-options").hidden = !local;
	$("#prod-remote-options").hidden = !linuxRemote;
	$("#prod-windows-options").hidden = !windows;
	$("#prod-protocol-hint").textContent = windows
		? "The protocol sets the site address used for the check after publishing. Bindings and certificates are managed in IIS."
		: "HTTPS issues a certificate, which requires DNS for the binding to point to the server. HTTP runs without a certificate.";
	prodForm.local_path.required = local;
	prodForm.host.required = linuxRemote;
	prodForm.user.required = linuxRemote;
	prodForm.ssh_port.required = linuxRemote;
	prodForm.smb_share.required = windows;
	prodForm.smb_user.required = windows;
	prodForm.smb_password.required = windows;
	const moving = Boolean(prodForm.dataset.moveTarget);
	const copying = Boolean(prodForm.dataset.copy);
	const release = target === "release";
	const same = target === "same";
	const sameServerMove = moving && !copying && local && prodForm.dataset.moveMode === "local";
	const noSettings = sameServerMove || release;
	$("#prod-folder-row").hidden = moving && local || noSettings;
	$("#prod-folder-label").textContent = windows ? "Folder name" : "Folder / service name";
	// On Windows the IIS site's folder is the share and the folder name, shown in full
	const share = prodForm.smb_share.value.trim().replace(/\\/g, "/").replace(/\/+$/, "");
	const folderName = prodForm.folder.value.trim();
	$("#prod-windows-path").textContent = `IIS site folder: ${share || "//server/share"}${share && share.split("/").pop().toLowerCase() === folderName.toLowerCase() ? "" : "/" + (folderName || "…")}`;
	$("#prod-binding-row").hidden = noSettings && !sameServerMove;
	$("#prod-environment-row").hidden = noSettings;
	$("#prod-protocol-row").hidden = noSettings;
	$("#prod-protocol-hint").hidden = noSettings;
	prodForm.folder.required = !(moving && local) && !noSettings;
	prodForm.binding.required = !release;
	const fromWindows = prodForm.dataset.moveMode === "windows";
	const hasSql = fromWindows || prodForm.dataset.moveSql === "1";
	const copyOptions = copying && !release;
	$("#prod-copy-options").hidden = !copyOptions;
	$("#prod-stop-row").hidden = prodForm.dataset.moveDatabase !== "SQLite";
	$("#prod-connection-row").hidden = !hasSql;
	const newConnection = copyOptions && hasSql && prodForm.elements.change_connection.checked;
	$("#prod-sql-fields").hidden = !newConnection;
	$("#prod-sql-fields").disabled = !newConnection;
	$("#prod-delete-row").hidden = copying;
	$("#prod-move-hint").textContent = windows
		? "Settings and content are copied, and the project is built for Windows. A connection to the source's own SQL Server is changed to its address, which must accept connections from the new server. With a Windows target, the IIS site and its folder must already exist."
		: fromWindows
			? "Settings and content are copied from Windows while its site runs, and the project is built for the destination. A connection to the Windows server's own SQL Server is changed to its address."
			: `${copying ? "The whole folder is copied" : "The site is stopped while the whole folder is copied"}, including its build, media, logs and a SQLite database, so it runs the same version. A connection to the source's own SQL Server is changed to its address, which must accept connections from the new server. With HTTPS, DNS must already point to the new server.`;
	const destinationServer = same ? prodForm.dataset.moveHost : target === "remote" ? prodForm.host.value.trim() || "…" : "";
	const destinationPath = windows ? prodForm.smb_share.value.trim() || "…"
		: local ? prodForm.local_path.value.trim() || "…"
			: `${destinationServer}:/var/www/${prodForm.folder.value.trim() || "…"}`;
	$("#prod-dest-path").hidden = !moving || release;
	$("#prod-dest-path").textContent = `${copying ? "Copied" : "Migrated"} to ${destinationPath}`;
	$("#prod-move-options").hidden = !moving || sameServerMove || release;
	prodForm.elements.delete_local.disabled = copying;
	$("#prod-release-hint").hidden = !release;
	$("#prod-submit").textContent = !moving ? "Add environment" : release ? "Move out" : copying ? "Copy" : "Migrate";
	$("#prod-local-hint").textContent = sameServerMove
		? "The folder is moved, and the service and Apache config follow it. It must be a new folder directly under /var/www, which also becomes the site's name. The port is unchanged. A new binding with HTTPS gets a new certificate, so DNS must point here first."
		: moving ? "It must be a new folder directly under /var/www, which also becomes the site's name."
		: "Use a subfolder of this site's folder, or /var/www/<production folder> to keep it outside the main site's folder.";
	const previous = prodForm.dataset.target;
	if (previous && previous !== target) {
		const defaultsFor = (name) => name === "local"
			? { folder: prodForm.dataset.localFolder, binding: prodForm.dataset.localBinding }
			: { folder: prodForm.dataset.remoteFolder, binding: prodForm.dataset.remoteBinding };
		const previousDefaults = defaultsFor(previous);
		const nextDefaults = defaultsFor(target);
		if (prodForm.folder.value === previousDefaults.folder) prodForm.folder.value = nextDefaults.folder;
		if (!prodForm.binding.dataset.edited && prodForm.binding.value === previousDefaults.binding) prodForm.binding.value = nextDefaults.binding;
	}
	prodForm.dataset.target = target;
}
function openCreateProdDialog(site, moveEnv, copying = false) {
	prodForm.reset();
	delete prodForm.dataset.copy;
	if (copying) prodForm.dataset.copy = "1";
	delete prodForm.dataset.moveDatabase;
	delete prodForm.dataset.moveSql;
	delete prodForm.dataset.moveHost;
	if (moveEnv) {
		prodForm.dataset.moveDatabase = moveEnv.database || "";
		prodForm.dataset.moveSql = ["MS SQL", "SQL Express", "Azure SQL"].includes(moveEnv.database) ? "1" : "";
		prodForm.dataset.moveHost = moveEnv.host || "";
	}
	delete prodForm.binding.dataset.edited;
	delete prodForm.dataset.moveTarget;
	delete prodForm.dataset.moveMode;
	if (moveEnv) {
		prodForm.dataset.moveTarget = moveEnv.target;
		prodForm.dataset.moveMode = moveEnv.mode;
	}
	$("#prod-title").textContent = moveEnv ? `${copying ? "Copy" : "Migrate"} ${environmentLabel(site, moveEnv)}` : "Add environment";
	$("#prod-description").textContent = moveEnv
		? copying
			? `Copy ${bindingLabel(moveEnv) || moveEnv.unit || moveEnv.folder} to a new folder or server. It stays as it is, and the copy becomes a new environment here.`
			: `Migrate ${bindingLabel(moveEnv) || moveEnv.unit || moveEnv.folder} to another folder or server. Everything goes with it.`
		: "Configure where this site is published to, then publish and sync content automatically when available.";
	// Migrating adds the choices of staying on the same server or becoming a separate site again
	const targets = $("#prod-target-row");
	for (const value of ["release", "same"]) targets.querySelector(`input[value=${value}]`)?.closest("label").remove();
	const targetCard = (value, title, sub) => el("label", { className: "type-card" },
		el("input", { type: "radio", name: "target", value }), el("span", { className: "tc-title", textContent: title }), el("span", { className: "tc-sub", textContent: sub }));
	if (moveEnv?.mode === "remote") targets.prepend(targetCard("same", "Same server", moveEnv.host || "Another folder there"));
	if (moveEnv?.separate && !copying) targets.append(targetCard("release", "Separate site", "Move it out again"));
	const main = site.environments.find((env) => !isTarget(env));
	const binding = (main && bindingLabel(main)) || siteLabel(site);
	prodForm.folder.value = siteLabel(site);
	prodForm.binding.value = binding;
	prodForm.dataset.siteName = site.name;
	prodForm.dataset.siteLabel = siteLabel(site);
	prodForm.local_path.value = `/var/www/${site.name}/_production`;
	prodForm.dataset.remoteFolder = siteLabel(site);
	prodForm.dataset.remoteBinding = binding;
	prodForm.dataset.localFolder = `production-${siteLabel(site)}`;
	prodForm.dataset.localBinding = `production-${siteLabel(site)}`;
	prodForm.elements.target.value = moveEnv?.separate && !copying ? "release" : moveEnv?.mode === "remote" ? "same" : "local";
	prodForm.elements.https.value = main && main.scheme === "http" ? "n" : "y";
	if (moveEnv) {
		// A copy needs its own name and binding, a migration keeps them unless they are changed
		const name = copying ? "" : moveEnv.unit || moveEnv.folder || siteLabel(site);
		const movedBinding = copying ? "" : bindingLabel(moveEnv) || binding;
		prodForm.folder.value = name;
		prodForm.binding.value = movedBinding;
		if (!copying) prodForm.binding.dataset.edited = "1";
		prodForm.local_path.value = `/var/www/${name}`;
		prodForm.dataset.remoteFolder = name;
		prodForm.dataset.remoteBinding = movedBinding;
		prodForm.dataset.localFolder = name;
		prodForm.dataset.localBinding = movedBinding;
		prodForm.elements.https.value = moveEnv.scheme === "http" ? "n" : "y";
		const current = [...prodForm.elements.environment.options].find((option) => option.value === moveEnv.environment);
		if (current) prodForm.elements.environment.value = current.value;
	}
	// Another environment on a Windows server gives the share and login it was set up with
	const windowsEnv = !moveEnv && site.environments.find((env) => env.mode === "windows" && env.folder);
	if (windowsEnv) {
		prodForm.smb_share.value = windowsEnv.folder.split("/").slice(0, -1).join("/");
		if (windowsEnv.user) prodForm.smb_user.value = windowsEnv.user;
	}
	// The fields start with the remote defaults: a new environment on this server gets its own folder and binding
	prodForm.dataset.target = moveEnv ? prodForm.elements.target.value : "remote";
	syncProdForm();
	prodForm.dataset.site = site.name;
	prodDialog.showModal();
	// The dialog opens without a card in focus, so only the chosen one is marked
	document.activeElement?.blur();
}
$("#prod-target-row").addEventListener("change", syncProdForm);
// A local environment gets its own folder and site name, so the defaults follow the environment
prodForm.elements.environment.onchange = () => {
	const name = prodForm.elements.environment.value.toLowerCase();
	const nextFolder = `${name}-${prodForm.dataset.siteLabel}`;
	const local = prodForm.elements.target.value === "local";
	if (local && prodForm.folder.value === prodForm.dataset.localFolder) prodForm.folder.value = nextFolder;
	if (local && !prodForm.binding.dataset.edited && prodForm.binding.value === prodForm.dataset.localBinding) prodForm.binding.value = nextFolder;
	if (!prodForm.dataset.moveTarget && /\/_[a-z]+$/.test(prodForm.local_path.value)) prodForm.local_path.value = `/var/www/${prodForm.dataset.siteName}/_${name}`;
	prodForm.dataset.localFolder = nextFolder;
	prodForm.dataset.localBinding = nextFolder;
};
prodForm.folder.oninput = () => {
	if (!prodForm.binding.dataset.edited) prodForm.binding.value = prodForm.folder.value;
	syncProdForm();
};
prodForm.local_path.oninput = () => {
	if (prodForm.dataset.copy && !prodForm.binding.dataset.edited) prodForm.binding.value = prodForm.local_path.value.trim().split("/").pop();
	syncProdForm();
};
prodForm.host.oninput = syncProdForm;
prodForm.smb_share.oninput = syncProdForm;
prodForm.elements.change_connection.onchange = syncProdForm;
prodForm.binding.oninput = () => { prodForm.binding.dataset.edited = "1"; };
$("#prod-cancel").onclick = () => prodDialog.close();
prodForm.onsubmit = (event) => {
	event.preventDefault();
	if (prodForm.elements.target.value === "release" && !prodForm.dataset.copy) {
		const release = { kind: "release_site", site: prodForm.dataset.site, target: prodForm.dataset.moveTarget };
		run(async () => {
			await openTask(release);
			prodDialog.close();
		});
		return;
	}
	const data = Object.fromEntries(new FormData(prodForm));
	data.kind = prodForm.dataset.moveTarget ? "move_env" : "create_prod";
	if (prodForm.dataset.moveTarget) data.environment_id = prodForm.dataset.moveTarget;
	if (prodForm.dataset.copy) {
		data.copy = true;
		data.stop_source = prodForm.elements.stop_source.checked && prodForm.dataset.moveDatabase === "SQLite";
		if (!$("#prod-sql-fields").hidden) {
			data.connection = { server: data.conn_server.trim(), database: data.conn_database.trim(), username: data.conn_username.trim(), password: data.conn_password };
		}
	}
	for (const key of ["conn_server", "conn_database", "conn_username", "conn_password", "change_connection"]) delete data[key];
	data.site = prodForm.dataset.site;
	run(async () => {
		await openTask(data);
		prodForm.reset();
		prodDialog.close();
	});
};

// Content can be synced from the main site or any environment to the other environments
// Data syncs from a site that can send to one that can receive, when both have it on, with what both take part with
const dataSyncKinds = (from, to) => {
	const source = from.data_sync || {}, destination = to.data_sync || {};
	if (!source.enabled || !destination.enabled || !source.send || !destination.receive) return [];
	return ["content", "media"].filter((kind) => source[kind] && destination[kind]);
};
function dataSyncDestinations(site, source) {
	return sortedEnvironments(site.environments).filter((env) => env !== source && !isMissingEnv(env) && dataSyncKinds(source, env).length);
}
function dataSyncSummary(env) {
	const settings = env.data_sync || {};
	const settingsText = [settings.settings_send !== false && "sends", isTarget(env) && settings.settings_receive !== false && "receives"].filter(Boolean).join(" and ");
	const prefix = `Settings: ${settingsText || "off"}. `;
	if (!settings.enabled) return prefix + "Data sync is off";
	const direction = [settings.send && "sends", settings.receive && "receives"].filter(Boolean).join(" and ") || "neither sends nor receives";
	const kinds = ["content", "media"].filter((kind) => settings[kind]).join(" and ") || "nothing";
	return `${prefix}Data sync: ${direction} ${kinds}`;
}

const dataSyncDialog = $("#dlg-data-sync");
const dataSyncForm = $("#form-data-sync");
function updateDataSyncForm() {
	const on = dataSyncForm.elements.enabled.checked;
	$("#data-sync-options").classList.toggle("off", !on);
	for (const name of ["send", "receive", "content", "media"]) dataSyncForm.elements[name].disabled = !on;
}
function openDataSyncDialog(site, env) {
	const settings = env.data_sync || {};
	dataSyncForm.dataset.site = site.name;
	dataSyncForm.dataset.id = isTarget(env) ? env.target : "main";
	$("#data-sync-site").textContent = `${environmentLabel(site, env)}: ${bindingLabel(env) || siteLabel(site)}`;
	for (const name of ["enabled", "send", "receive", "content", "media"]) dataSyncForm.elements[name].checked = !!settings[name];
	// Settings sync is on unless it was turned off. The main site is where settings come from: it never receives them.
	for (const name of ["settings_send", "settings_receive"]) dataSyncForm.elements[name].checked = settings[name] !== false;
	$("#data-sync-settings-receive").hidden = !isTarget(env);
	updateDataSyncForm();
	dataSyncDialog.showModal();
	// The dialog opens without a card in focus, so none of them looks chosen
	document.activeElement?.blur();
}
dataSyncForm.elements.enabled.onchange = () => {
	// Turning it on for the first time takes part with both
	if (dataSyncForm.elements.enabled.checked && !dataSyncForm.elements.content.checked && !dataSyncForm.elements.media.checked) {
		dataSyncForm.elements.content.checked = dataSyncForm.elements.media.checked = true;
	}
	updateDataSyncForm();
};
$("#data-sync-cancel").onclick = () => dataSyncDialog.close();
dataSyncForm.onsubmit = (event) => {
	event.preventDefault();
	const elements = dataSyncForm.elements;
	const settings = Object.fromEntries(["enabled", "send", "receive", "content", "media", "settings_send", "settings_receive"].map((name) => [name, elements[name].checked]));
	if (settings.enabled && !settings.content && !settings.media) {
		toast("Choose content, media or both.", "error");
		return;
	}
	const { site, id } = dataSyncForm.dataset;
	run(async () => {
		await api(`/api/sites/${encodeURIComponent(site)}/data-sync`, { json: { id, settings } });
		dataSyncDialog.close();
		await openTask({ kind: "data_sync_settings", site, id });
	});
};

// Settings go from a site that may send them to environments that may receive them, as set in Sync setup
function syncDestinations(site, source) {
	if (source.data_sync?.settings_send === false) return [];
	return sortedEnvironments(site.environments).filter((env) => env !== source && isTarget(env) && !isMissingEnv(env)
		&& (env.scripts || []).includes("sync_prod.sh") && env.data_sync?.settings_receive !== false);
}
// A folder put in /var/www by hand gets a service, an Apache config and a certificate, like Create site,
// without anything in it being overwritten
const setupSiteDialog = $("#dlg-setup-site");
const setupSiteForm = $("#form-setup-site");
const setupKindText = {
	"umbraco project": "an Umbraco project. It gets the site's scripts, and is built into _publish unless a build is there already.",
	"C# project": "a C# project. It gets the site's scripts, and is built into _publish unless a build is there already.",
	"umbraco build": "an Umbraco build. It runs as it is: nothing is built.",
	"C# build": "a C# build. It runs as it is: nothing is built.",
	html: "static files. It is set up as an HTML site.",
};
function openSetupSiteDialog(site) {
	setupSiteForm.reset();
	setupSiteForm.dataset.site = site.name;
	const build = site.setup_kind.endsWith("build");
	$("#setup-site-found").textContent = `${siteLabel(site)} holds ${setupKindText[site.setup_kind] || site.setup_kind}`;
	// A site whose setup was not finished keeps the binding its Apache config has
	const binding = site.environments?.[0]?.display_binding || site.environments?.[0]?.binding || "";
	setupSiteForm.binding.value = binding || (site.name.includes(".") ? siteLabel(site) : "");
	setupSiteForm.environment.value = build ? "Production" : "Test";
	$("#setup-site-environment-row").hidden = site.setup_kind === "html";
	$("#setup-site-hint").textContent = "Only the service, the Apache config, the certificate and missing scripts are created. Nothing in the folder is overwritten, but the folder the site runs from is owned by www-data, so the site can write there. With HTTPS, DNS for the binding must point to this server.";
	setupSiteDialog.showModal();
}
$("#setup-site-cancel").onclick = () => setupSiteDialog.close();
setupSiteForm.onsubmit = (event) => {
	event.preventDefault();
	const data = Object.fromEntries(new FormData(setupSiteForm));
	run(async () => {
		await openTask({ kind: "setup_site", site: setupSiteForm.dataset.site, binding: data.binding.trim(), certificate: data.certificate,
			environment: $("#setup-site-environment-row").hidden ? "" : data.environment });
		setupSiteDialog.close();
	});
};

const adoptDialog = $("#dlg-adopt");
const adoptForm = $("#form-adopt");
function openAdoptDialog(site) {
	adoptForm.dataset.site = site.name;
	$("#adopt-site").textContent = siteLabel(site);
	$("#adopt-main").replaceChildren(...site.adopt_into.map((name) => el("option", { value: name, textContent: name })));
	adoptDialog.showModal();
}
$("#adopt-cancel").onclick = () => adoptDialog.close();
adoptForm.onsubmit = (event) => {
	event.preventDefault();
	const data = { kind: "adopt_site", site: adoptForm.dataset.site, main: adoptForm.elements.main.value };
	run(async () => {
		await openTask(data);
		adoptDialog.close();
	});
};
function filterTargets() {
	const words = $("#targets-search").value.toLowerCase().split(/\s+/).filter(Boolean);
	let shown = 0;
	for (const panel of $("#env-targets").children) {
		panel.hidden = !words.every((word) => (panel.dataset.search || "").includes(word));
		if (!panel.hidden) shown += 1;
	}
	$("#targets-no-match").hidden = shown > 0 || !words.length;
}
$("#targets-search").oninput = filterTargets;
$("#targets-search").onkeydown = (event) => {
	if (event.key === "Escape" && event.target.value) {
		event.stopPropagation();
		event.target.value = "";
		filterTargets();
	}
};

const syncDialog = $("#dlg-sync");
const syncForm = $("#form-sync");
function openSyncDialog(site, source, data = false) {
	syncForm.dataset.site = site.name;
	syncForm.dataset.source = isTarget(source) ? source.target : "main";
	syncForm.dataset.data = data ? "1" : "";
	$("#sync-title").textContent = data ? "Sync data" : "Sync settings";
	$("#sync-submit").textContent = data ? "Sync data" : "Sync";
	$("#sync-hint-settings").hidden = data;
	$("#sync-hint-data").hidden = !data;
	$("#sync-source").textContent = data ? "Content and media are merged into the destination" : "uSync settings, Views, css and scripts";
	$("#sync-from-env").textContent = environmentLabel(site, source);
	$("#sync-from-env").className = "sync-env-name env-" + environmentLabel(site, source).toLowerCase();
	$("#sync-from-binding").textContent = bindingLabel(source) || siteLabel(site);
	const destinations = data ? dataSyncDestinations(site, source) : syncDestinations(site, source);
	// One card per destination, in the colour of its environment. Several can be chosen.
	const card = (value, title, sub, tone, chips = []) => el("label", { className: `type-card environment-option sync-choice env-${tone}` },
		el("input", { type: "checkbox", name: "destination", value, onchange: updateSyncSelection }),
		el("span", { className: "tc-title", textContent: title }),
		el("span", { className: "tc-sub", textContent: sub }),
		...(chips.length ? [el("span", { className: "sync-chips" }, ...chips.map((chip) => el("span", { className: "sync-chip", textContent: chip })))] : []));
	// The site's name first, its environment below in the environment's colour
	const cards = destinations.map((env) => card(isTarget(env) ? env.target : "main", bindingLabel(env) || env.host || env.target,
		environmentLabel(site, env), environmentLabel(site, env).toLowerCase(), data ? dataSyncKinds(source, env) : []));
	$("#sync-destination").replaceChildren(...cards);
	cards[0].querySelector("input").checked = true;
	$("#sync-select-all").hidden = cards.length < 2;
	updateSyncSelection();
	syncDialog.showModal();
}
$("#sync-cancel").onclick = () => syncDialog.close();
const syncChoices = () => [...$("#sync-destination").querySelectorAll("input[name=destination]")];
function updateSyncSelection() {
	const chosen = syncChoices().filter((input) => input.checked).length;
	const all = chosen === syncChoices().length;
	$("#sync-select-all").textContent = all ? "Select none" : "Select all";
	const verb = syncForm.dataset.data ? "Sync data" : "Sync";
	$("#sync-submit").textContent = chosen > 1 ? `${verb} to ${chosen} sites` : verb;
	$("#sync-submit").disabled = chosen === 0;
}
$("#sync-select-all").onclick = () => {
	const all = syncChoices().every((input) => input.checked);
	for (const input of syncChoices()) input.checked = !all;
	updateSyncSelection();
};
syncForm.onsubmit = (event) => {
	event.preventDefault();
	const destination = syncChoices().filter((input) => input.checked).map((input) => input.value);
	if (!destination.length) return;
	const data = { kind: syncForm.dataset.data ? "data_sync" : "sync_env", site: syncForm.dataset.site, source: syncForm.dataset.source, destination };
	run(async () => {
		await openTask(data);
		syncDialog.close();
	});
};

const environmentDialog = $("#dlg-environment");
const environmentForm = $("#form-environment");
function openEnvironmentDialog(site, env) {
	environmentForm.reset();
	environmentForm.dataset.site = site.name;
	environmentForm.dataset.scope = isTarget(env) ? "prod" : "test";
	environmentForm.dataset.target = env.target && env.target !== "prod" ? env.target : "";
	const choices = [...environmentForm.querySelectorAll("input[name=environment]")];
	const selected = choices.find((input) => input.value.toLowerCase() === (env.environment || "").toLowerCase());
	if (selected) selected.checked = true;
	$("#environment-site").textContent = bindingLabel(env) || siteLabel(site);
	$("#environment-current").textContent = env.environment || "Unknown";
	$("#environment-current").className = "badge env-" + (env.environment || "unknown").toLowerCase();
	environmentDialog.showModal();
	(selected || choices[0]).focus();
}
$("#environment-cancel").onclick = () => environmentDialog.close();
environmentForm.onsubmit = (event) => {
	event.preventDefault();
	const data = {
		kind: "prod_action", action: "change_env",
		site: environmentForm.dataset.site, scope: environmentForm.dataset.scope,
		environment: environmentForm.elements.environment.value,
		...(environmentForm.dataset.target ? { target: environmentForm.dataset.target } : {}),
	};
	run(async () => {
		await openTask(data);
		environmentDialog.close();
	});
};

const multiDialog = $("#dlg-multi");
const multiForm = $("#form-multi");
$("#btn-multi").onclick = () => { multiSelecting = true; renderSites(); };
$("#btn-multi-cancel").onclick = () => { multiSelecting = false; multiSites.clear(); renderSites(); };
function syncMultiAction() {
	const action = multiForm.elements.namedItem("action").value;
	$("#multi-action-description").textContent = {
		check: "Run dotnet list package --outdated for every selected project without changing packages.",
		add: "Add a NuGet package to every selected project.",
		remove: "Remove a NuGet package from every selected project.",
		local: "Build and restart each selected main site.",
		prod: "Build and deploy every environment of each selected site, one at a time per site.",
		both: "Publish each main site, then all its environments. Sites run in parallel.",
		outdated: "Run dotnet outdated --upgrade for every selected project. Review changes and publish afterward.",
		clear_logs: "Delete the Umbraco logs of each selected main site. Sites that are not Umbraco are skipped.",
		clear_logs_prod: "Delete the Umbraco logs of every environment of the selected sites. Sites that are not Umbraco, or have no environments, are skipped.",
		clear_logs_all: "Delete the Umbraco logs of each selected main site and all its environments. Sites that are not Umbraco are skipped.",
	}[action] || "";
	const packageAction = action === "add" || action === "remove";
	$("#multi-package-fields").hidden = !packageAction;
	multiForm.elements.namedItem("package").disabled = !packageAction;
	$("#multi-version-row").hidden = action !== "add";
	multiForm.elements.namedItem("version").disabled = action !== "add";
	const selected = listedSites().filter((site) => multiSites.has(site.name));
	const clearing = action.startsWith("clear_logs");
	const skipped = action === "prod" || action === "both"
		? selected.filter((site) => !site.scripts.includes("publish_prod.sh"))
		: action === "clear_logs_prod" ? selected.filter((site) => site.kind !== "umbraco" || !site.environments.some(isTarget))
		: clearing ? selected.filter((site) => site.kind !== "umbraco") : [];
	$("#multi-skipped").textContent = skipped.length
		? `Will be skipped (${clearing ? (action === "clear_logs_prod" ? "not Umbraco, or no environments" : "not Umbraco") : "no environments"}): ` + skipped.map(siteLabel).join(", ") : "";
	$("#multi-submit").disabled = !action || submittingMulti || !selected.length || skipped.length === selected.length;
}
$("#btn-multi-all").onclick = () => {
	const shownSites = overviewSites().filter(matches);
	const all = shownSites.every((site) => multiSites.has(site.name));
	for (const site of shownSites) {
		if (all) multiSites.delete(site.name);
		else multiSites.add(site.name);
	}
	renderSites();
};
$("#btn-multi-action").onclick = () => {
	multiForm.reset();
	$("#multi-selection").replaceChildren(...Array.from(multiSites, (name) =>
		el("span", { className: "multi-site-tag", role: "listitem" }, svgIcon("globe"), el("span", { textContent: name }))));
	$("#multi-count").textContent = `${multiSites.size} site${multiSites.size === 1 ? "" : "s"} selected. Choose what to do with them.`;
	// Clearing logs is for Umbraco sites
	$("#multi-maintenance").hidden = !listedSites().some((site) => multiSites.has(site.name) && site.kind === "umbraco");
	syncMultiAction();
	multiDialog.showModal();
};
multiForm.addEventListener("change", (event) => { if (event.target.name === "action") syncMultiAction(); });
$("#multi-close").onclick = () => multiDialog.close();
let submittingMulti = false;
multiForm.onsubmit = (event) => {
	event.preventDefault();
	if (submittingMulti) return;
	const payload = Object.fromEntries(new FormData(multiForm));
	payload.kind = "bulk";
	payload.sites = Array.from(multiSites);
	submittingMulti = true;
	$("#multi-submit").disabled = true;
	run(async () => {
		try {
			await openTask(payload);
			multiDialog.close();
			multiSelecting = false;
			multiSites.clear();
			renderSites();
		} finally {
			submittingMulti = false;
			syncMultiAction();
		}
	});
};

const packageDialog = $("#dlg-package");
const packageForm = $("#form-package");
function openPackageDialog(site, action) {
	packageForm.reset();
	packageForm.dataset.site = site.name;
	packageForm.dataset.action = action;
	const adding = action === "add";
	$("#package-title").textContent = adding ? "Add package" : "Remove package";
	$("#package-description").textContent = adding
		? "Add a NuGet package to this site's project."
		: "Remove a NuGet package from this site's project.";
	$("#package-version-row").hidden = !adding;
	$("#package-submit").textContent = adding ? "Add package" : "Remove package";
	packageDialog.showModal();
	packageForm.elements.namedItem("package").focus();
}
$("#package-cancel").onclick = () => packageDialog.close();
packageForm.onsubmit = (event) => {
	event.preventDefault();
	const data = Object.fromEntries(new FormData(packageForm));
	data.kind = "dotnet_package";
	data.site = packageForm.dataset.site;
	data.action = packageForm.dataset.action;
	run(async () => {
		await openTask(data);
		packageDialog.close();
	});
};

// ------------------------------------------------------------------ files

var currentPath = "";
const editorDialog = $("#dlg-editor");
const filesDialog = $("#dlg-files");
$("#files-close").onclick = () => filesDialog.close();
filesDialog.addEventListener("cancel", (event) => {
	if (event.target !== filesDialog) return;
	event.preventDefault();
	filesDialog.close();
});
let openFile = null;
let configurationEditor = null;
let savingFile = false;
let dirty = false;

const sizeText = (n) => (n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB");
const join = (a, b) => (a ? a + "/" + b : b);
const parentOf = (path) => path.split("/").slice(0, -1).join("/");

function setDirty(value) {
	dirty = value;
	$("#editor-dirty").hidden = !value;
}
window.addEventListener("beforeunload", (event) => { if (dirty) event.preventDefault(); });

async function discardOk() {
	return !dirty || (await ask({ title: "Unsaved changes", message: `${openFile} has unsaved changes.`, input: false, ok: "Discard", danger: true })) === true;
}

let hideNonEditableFiles = true;
let listedFiles = [];
let filesLoadGeneration = 0;
function filesPathPrefix() {
	return remoteFilesSite || serverFiles ? "/" : "/var/www/" + localFilesRoot + "/";
}
function filesSearchName() {
	return $("#files-search").value.split("/").pop();
}
$("#files-search").addEventListener("beforeinput", (event) => {
	const field = event.target;
	const limit = filesPathPrefix().length;
	const start = field.selectionStart;
	const end = field.selectionEnd;
	const deleting = event.inputType.startsWith("delete");
	if (start >= limit && !(deleting && event.inputType.includes("Backward") && start === limit && start === end)) return;
	event.preventDefault();
	if (deleting) {
		if (end > limit) field.setRangeText("", limit, end, "end");
	} else if (event.data !== null) {
		field.setRangeText(event.data, Math.max(limit, start), Math.max(limit, end), "end");
	}
	field.dispatchEvent(new Event("input", { bubbles: true }));
});
$("#files-search").addEventListener("paste", (event) => {
	const field = event.target;
	const prefix = filesPathPrefix();
	if (field.selectionStart >= prefix.length) return;
	event.preventDefault();
	const text = event.clipboardData.getData("text");
	field.setRangeText(text.startsWith(prefix) ? text.slice(prefix.length) : text,
		prefix.length, Math.max(prefix.length, field.selectionEnd), "end");
	field.dispatchEvent(new Event("input", { bubbles: true }));
});
function focusFilesPath() {
	if (!filesDialog.open || [...document.querySelectorAll("dialog:modal")].some((dialog) => dialog !== filesDialog)) return;
	const field = $("#files-search");
	field.focus({ preventScroll: true });
	field.setSelectionRange(field.value.length, field.value.length);
}
$("#drop").addEventListener("click", () => setTimeout(focusFilesPath, 0));
document.addEventListener("close", () => setTimeout(focusFilesPath, 0), true);
document.addEventListener("keydown", (event) => {
	if (!filesDialog.open || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey
		|| [...document.querySelectorAll("dialog:modal")].some((dialog) => dialog !== filesDialog)
		|| event.target.closest("input, textarea, select, [contenteditable]")
		|| (event.key.length !== 1 && event.key !== "Backspace")) return;
	event.preventDefault();
	focusFilesPath();
	const field = $("#files-search");
	const end = field.value.length;
	field.setRangeText(event.key === "Backspace" ? "" : event.key, event.key === "Backspace" ? Math.max(filesPathPrefix().length, end - 1) : end, end, "end");
	field.dispatchEvent(new Event("input", { bubbles: true }));
});
async function navigateFilesInput(enter = false) {
	const field = $("#files-search");
	if (!field.value) field.value = filesPathPrefix();
	if (!field.value.startsWith("/")) field.value = filesPathPrefix() + field.value;
	const value = field.value;
	renderFiles();
	if (!enter && !value.includes("/")) return;
	const split = value.lastIndexOf("/");
	let path = enter ? value : value.slice(0, split + 1);
	const suffix = enter ? "" : value.slice(split + 1);
	if (path.startsWith("/")) {
		const prefix = filesPathPrefix();
		if (path === prefix.slice(0, -1) || path === prefix) {
			path = "";
		} else {
			if (!path.startsWith(prefix)) {
				throw new Error(`This file browser is confined to ${prefix}`);
			}
			path = path.slice(prefix.length);
		}
	} else {
		path = join(currentPath, path);
	}
	path = path.replace(/\/+$/, "");
	if (!enter && path === currentPath) return;
	await loadFiles(path, suffix);
}
$("#files-search").oninput = () => {
	filesLoadGeneration++;
	run(() => navigateFilesInput());
};
async function completeFilesInput() {
	const field = $("#files-search");
	const value = field.value;
	await navigateFilesInput();
	if (field.value !== value) return;
	const name = filesSearchName();
	const candidates = listedFiles.filter((item) => item.name.startsWith(name));
	if (!candidates.length) return;
	let completion = candidates[0].name;
	for (const item of candidates.slice(1)) {
		let length = 0;
		while (length < completion.length && completion[length] === item.name[length]) length++;
		completion = completion.slice(0, length);
	}
	field.value = value.slice(0, value.length - name.length) + completion
		+ (candidates.length === 1 && candidates[0].dir ? "/" : "");
	field.setSelectionRange(field.value.length, field.value.length);
	filesLoadGeneration++;
	await navigateFilesInput();
}
$("#files-search").onkeydown = (event) => {
	if (event.key === "Tab" && !event.shiftKey
		&& event.target.selectionStart === event.target.value.length
		&& event.target.selectionEnd === event.target.value.length) {
		event.preventDefault();
		run(completeFilesInput);
	} else if (event.key === "Enter") {
		event.preventDefault();
		run(() => navigateFilesInput(true));
	}
};
$("#files-filter").onclick = () => {
	hideNonEditableFiles = !hideNonEditableFiles;
	$("#files-filter").textContent = hideNonEditableFiles ? "Show all files" : "Hide non-editable files";
	$("#files-filter").setAttribute("aria-pressed", String(hideNonEditableFiles));
	renderFiles();
};

async function loadFiles(path, suffix = "") {
	const generation = ++filesLoadGeneration;
	const data = await api(fsUrl("list") + "?path=" + encodeURIComponent(path));
	if (generation !== filesLoadGeneration) return;
	currentPath = data.path;
	// Windows has no owner or rights to show
	$("#files").classList.toggle("files-windows", !!data.windows);
	$("#files-title").dataset.windows = data.windows ? "1" : "";
	$("#files-search").value = filesPathPrefix() + (currentPath ? currentPath + "/" : "") + suffix;
	listedFiles = data.items;
	renderFiles();
	focusFilesPath();
}

function renderFiles() {
	const query = filesSearchName().trim().toLocaleLowerCase();
	const items = listedFiles.filter((item) => (!hideNonEditableFiles || (item.dir ? item.has_editable_files : item.editable))
		&& item.name.toLocaleLowerCase().includes(query));
	const body = $("#files tbody");
	body.replaceChildren();
	if (currentPath) {
		const openParent = () => run(() => loadFiles(parentOf(currentPath)));
		body.append(el("tr", { className: "file-folder-row", onclick: (event) => {
			if (!event.target.closest("button")) openParent();
		} }, el("td", {}, el("button", { type: "button", className: "file-entry file-name", ariaLabel: "Open parent folder", onclick: openParent }, svgIcon("back"), el("span", { textContent: ".." }))), ...Array.from({ length: 5 }, () => el("td"))));
	}
	for (const item of items) {
		const full = join(currentPath, item.name);
		const canOpen = item.dir || item.editable;
		const link = el(canOpen ? "button" : "span", {
			className: `file-entry ${item.dir ? "file-folder" : "file-document"} ${canOpen ? "file-name" : "file-uneditable"}`,
			...(canOpen ? { type: "button", onclick: () => run(() => (item.dir ? loadFiles(full) : editFile(full))) } : {}),
		}, svgIcon(item.dir ? "folder" : "file"), el("span", { textContent: item.name }));
		const actions = el("div", { className: "file-tools" });
		const tools = el("td", { className: "tools" }, actions);
		if (!item.dir) {
			const download = el("a", { className: "file-action file-download", href: fsUrl("download") + "?path=" + encodeURIComponent(full) }, svgIcon("download"));
			download.setAttribute("aria-label", `Download ${item.name}`);
			actions.append(download);
		}
		for (const [label, icon, tone, action] of [
			["Rename", "pencil", "amber", () => renameItem(full, item.name)],
			["Delete", "trash", "red", () => deleteItem(full, item.name)],
		]) {
			const button = el("button", { type: "button", className: "file-action", onclick: () => run(action) }, svgIcon(icon));
			button.setAttribute("aria-label", `${label} ${item.name}`);
			button.dataset.tone = tone;
			actions.append(button);
		}
		body.append(el("tr", canOpen ? { className: "file-open-row", onclick: (event) => {
			if (!event.target.closest(".tools, button, a")) run(() => item.dir ? loadFiles(full) : editFile(full));
		} } : {}, el("td", {}, link),
		el("td", { className: "file-size num muted", textContent: item.dir ? "" : sizeText(item.size) }),
		el("td", { className: "file-modified muted", textContent: formatDate(item.modified * 1000) }),
		el("td", { className: "file-permissions muted", textContent: item.permissions }),
		el("td", { className: "file-owner muted", textContent: item.owner }), tools));
	}
	if (!items.length) body.append(el("tr", { className: "file-empty-row" }, el("td", { colSpan: 6, className: "empty", textContent: query ? "No matching files or folders." : listedFiles.length ? "No editable files in this folder." : "This folder is empty." })));
}

async function openFiles(path, remoteSite = "", server = false) {
	remoteFilesSite = remoteSite;
	serverFiles = server && !remoteSite;
	localFilesRoot = remoteSite || serverFiles ? "" : path.replace(/^\/var\/www\//, "").replace(/\/+$/, "");
	$("#files-search").value = "";
	await loadFiles(remoteSite || serverFiles ? path : "");
	$("#files-title").textContent = remoteSite ? `${$("#files-title").dataset.windows ? "Windows" : "Remote"} files · ${remoteLabel(remoteSite)}` : serverFiles ? "Server files" : "Files";
	if (!filesDialog.open) filesDialog.showModal();
	focusFilesPath();
}

// Monaco is downloaded into static/vendor by install/update_admin_site.sh; without it a plain textarea is used
let monacoPromise = null;
let monacoEditor = null;
function loadMonaco() {
	if (monacoPromise) return monacoPromise;
	monacoPromise = new Promise((resolve, reject) => {
		const base = location.origin + "/static/vendor/monaco/";
		const workerUrl = URL.createObjectURL(new Blob([`self.MonacoEnvironment={baseUrl:'${base}'};importScripts('${base}vs/base/worker/workerMain.js');`], { type: "text/javascript" }));
		window.MonacoEnvironment = { getWorkerUrl: () => workerUrl };
		document.head.append(el("script", {
			src: "/static/vendor/monaco/vs/loader.js",
			onload: () => {
				window.require.config({ paths: { vs: "/static/vendor/monaco/vs" } });
				window.require(["vs/editor/editor.main"], () => resolve(window.monaco), reject);
			},
			onerror: () => reject(new Error("Monaco is not installed")),
		}));
	});
	return monacoPromise;
}

const languageByExtension = { conf: "ini", service: "ini", cshtml: "razor", csproj: "xml", props: "xml", config: "xml", sh: "shell", env: "ini", cs: "csharp", yml: "yaml" };
function languageFor(path) {
	const name = path.split("/").pop().toLowerCase();
	return languageByExtension[name.includes(".") ? name.split(".").pop() : ""];
}

let loadingContent = false;
let openFileSaveUrl = "";
async function showInEditor(path, content) {
	let monaco = null;
	try { monaco = await loadMonaco(); } catch (_) { monaco = null; }
	const box = $("#editor-box");
	const area = $("#editor-text");
	if (!monaco) {
		box.hidden = true;
		area.hidden = false;
		area.value = content;
		return;
	}
	area.hidden = true;
	box.hidden = false;
	if (!monacoEditor) {
		monacoEditor = monaco.editor.create(box, { theme: "vs-dark", automaticLayout: true, fontSize: 13, minimap: { enabled: true }, scrollBeyondLastLine: false, renderWhitespace: "selection", tabSize: 4, detectIndentation: true });
		monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => run(saveFile));
	}
	const previous = monacoEditor.getModel();
	loadingContent = true;
	try {
		monacoEditor.setModel(null);
		if (previous) previous.dispose();
		const model = monaco.editor.createModel(content, languageFor(path), monaco.Uri.file("/" + path));
		monacoEditor.setModel(model);
		model.onDidChangeContent(() => { if (!loadingContent) setDirty(true); });
	} finally {
		loadingContent = false;
	}
	monacoEditor.focus();
}

const editorValue = () => (monacoEditor && !$("#editor-box").hidden ? monacoEditor.getValue() : $("#editor-text").value);

async function editFile(path, remoteSite = null) {
	if (!(await discardOk())) return;
	const base = remoteSite === null ? fsUrl("") : remoteSite ? "/api/remote-fs/" + encodeURIComponent(remoteSite) + "/" : "/api/fs/";
	const saveUrl = base + "save";
	const data = await api(base + "read?path=" + encodeURIComponent(path));
	openFile = path;
	configurationEditor = null;
	openFileSaveUrl = saveUrl;
	if (!editorDialog.open) editorDialog.showModal();
	$("#editor-path").textContent = path;
	$("#editor-status").textContent = "";
	await showInEditor(path, data.content);
	setDirty(false);
}

async function editConfiguration(site, env, kind) {
	if (!(await discardOk())) return;
	const url = `/api/sites/${encodeURIComponent(site.name)}/config/${kind}?scope=${env.mode === "remote" ? "remote" : "local"}&unit=${encodeURIComponent(env.unit)}${env.target && env.target !== "prod" ? "&target=" + encodeURIComponent(env.target) : ""}`;
	let data = await api(url);
	if (data.encoding_required) {
		const dialog = $("#dlg-encoding");
		$("#encoding-message").textContent = data.message;
		dialog.returnValue = "";
		const encoding = await new Promise((resolve) => {
			dialog.onclose = () => resolve(dialog.returnValue === "open" ? $("#config-encoding").value : null);
			dialog.showModal();
		});
		if (!encoding) return;
		data = await api(url + "&encoding=" + encodeURIComponent(encoding));
	}
	configurationEditor = { url, kind, encoding: data.encoding || "utf-8" };
	openFile = data.path;
	if (!editorDialog.open) editorDialog.showModal();
	$("#editor-path").textContent = data.path + (env.mode === "remote" ? " · " + env.host : "");
	$("#editor-status").textContent = configurationEditor.encoding === "utf-8" ? ""
		: `Opened as ${configurationEditor.encoding}. Check the characters before saving; Save converts this file to UTF-8.`;
	await showInEditor(data.path, data.content);
	setDirty(false);
}

async function saveFile() {
	if (openFile === null || savingFile) return;
	savingFile = true;
	$("#editor-save").disabled = true;
	try {
		const configuration = configurationEditor;
		const content = editorValue();
		if (configuration && configuration.encoding !== "utf-8" && !(await ask({
			title: "Convert configuration to UTF-8?",
			message: `This file was opened as ${configuration.encoding}. Verify that its characters look correct. Saving converts the file to UTF-8.`,
			input: false, ok: "Save as UTF-8",
		}))) return;
		await api(configuration ? configuration.url : openFileSaveUrl, {
			json: { action: "save", path: openFile, content },
		});
		if (configuration) configuration.encoding = "utf-8";
		setDirty(editorValue() !== content);
		$("#editor-status").textContent = "Saved " + formatDate(new Date());
		if (configuration) {
			const service = configuration.kind === "service";
			const apply = await ask({
				title: service ? "Reload daemon and restart service?" : "Test config and reload Apache?",
				message: service ? "The file has been saved. Restart the service to apply the changes."
					: "The file has been saved. Apache will only reload if the configuration test succeeds.",
				input: false, ok: service ? "Reload and restart" : "Test and reload",
			});
			if (apply) {
				$("#editor-status").textContent = "Applying changes…";
				await api(configuration.url, { json: { action: "apply" } });
				$("#editor-status").textContent = service ? "Saved. Service restarted." : "Saved. Config test passed; Apache reloaded.";
				await loadSites();
			}
		}
	} catch (error) {
		$("#editor-status").textContent = error.message;
		toast(error.message, "error");
	} finally {
		savingFile = false;
		$("#editor-save").disabled = false;
	}
}

async function createItem(kind, title) {
	const name = await ask({ title, placeholder: "Name", ok: "Create" });
	if (!name) return;
	await api(fsUrl(kind), { json: { path: currentPath, name: name.trim() } });
	await loadFiles(currentPath);
}

async function uploadFiles(files) {
	if (!files.length) return;
	const form = new FormData();
	form.append("path", currentPath);
	for (const file of files) form.append("files", file);
	const result = await api(fsUrl("upload"), { method: "POST", body: form });
	toast("Uploaded " + result.count + " file(s)");
	await loadFiles(currentPath);
}

async function renameItem(path, name) {
	const next = await ask({ title: "Rename", value: name, ok: "Rename" });
	if (!next || next === name) return;
	await api(fsUrl("rename"), { json: { path, name: next } });
	await loadFiles(currentPath);
}

async function deleteItem(path, name) {
	if (!(await ask({ title: "Delete " + name + "?", message: "This permanently deletes the file or folder.", input: false, ok: "Delete", danger: true }))) return;
	await api(fsUrl("delete"), { json: { path } });
	await loadFiles(currentPath);
}

$("#btn-new-file").onclick = () => run(() => createItem("newfile", "New file"));
$("#btn-new-folder").onclick = () => run(() => createItem("mkdir", "New folder"));
$("#btn-upload").onclick = () => $("#upload-input").click();
$("#upload-input").onchange = (event) => {
	const files = [...event.target.files];
	event.target.value = "";
	run(() => uploadFiles(files));
};

let fileDragDepth = 0;
function resetFileDrag() {
	fileDragDepth = 0;
	drop.classList.remove("dragging");
}
drop.addEventListener("dragenter", (event) => {
	event.preventDefault();
	fileDragDepth++;
	drop.classList.add("dragging");
});
drop.addEventListener("dragover", (event) => { event.preventDefault(); });
drop.addEventListener("dragleave", () => {
	fileDragDepth = Math.max(0, fileDragDepth - 1);
	if (!fileDragDepth) resetFileDrag();
});
document.addEventListener("dragend", resetFileDrag);
filesDialog.addEventListener("close", resetFileDrag);
drop.addEventListener("drop", (event) => {
	event.preventDefault();
	resetFileDrag();
	run(() => uploadFiles([...event.dataTransfer.files]));
});
$("#editor-save").onclick = () => run(saveFile);
async function closeEditor() {
	if (savingFile) return;
	if (!(await discardOk())) return;
	openFile = null;
	configurationEditor = null;
	setDirty(false);
	editorDialog.close();
}
$("#editor-close").onclick = closeEditor;
editorDialog.addEventListener("keydown", (event) => {
	if (event.key !== "Escape" || savingFile) return;
	event.preventDefault();
	event.stopImmediatePropagation();
	if (!event.repeat) run(closeEditor);
}, true);
editorDialog.addEventListener("cancel", (event) => {
	event.preventDefault();
	if (event.target === editorDialog) run(closeEditor);
});
$("#editor-text").oninput = () => setDirty(true);
$("#editor-text").onkeydown = (event) => {
	if ((event.ctrlKey || event.metaKey) && event.key === "s") { event.preventDefault(); run(saveFile); }
	if (event.key === "Tab" && !event.shiftKey) {
		event.preventDefault();
		const area = event.target;
		area.setRangeText("\t", area.selectionStart, area.selectionEnd, "end");
		setDirty(true);
	}
};

// -------------------------------------------------------------- terminals

const terminals = {};
let activeTerminal = null;

function updateTerminalState() {
	const count = Object.keys(terminals).length;
	$("#term-count").textContent = count;
	$("#term-count").hidden = !count;
	$("#btn-dock").hidden = !count;
	if (!count) $("#dock").hidden = true;
}

function showDock() {
	$("#dock").hidden = false;
	fitActive();
}

function toggleDock() {
	if (!$("#dock").hidden) { $("#dock").hidden = true; return; }
	if (!Object.keys(terminals).length) { toast("No terminals open. Open a shell or run a script from a site."); return; }
	showDock();
}
$("#btn-dock").onclick = toggleDock;
// Closing the window also removes the terminals whose process has finished; running ones keep going
async function closeDock() {
	for (const t of Object.values(terminals).filter((x) => x.exited)) await t.remove();
	$("#dock").hidden = true;
}
$("#dock-close").onclick = closeDock;
document.addEventListener("keydown", (event) => {
	if (event.key !== "Escape" || $("#dock").hidden || document.querySelector("dialog[open]")) return;
	event.preventDefault();
	event.stopImmediatePropagation();
	if (!event.repeat) run(closeDock);
}, true);
$("#dock-size").onclick = () => { $("#dock").classList.toggle("max"); fitActive(); };

function fitActive() {
	const t = terminals[activeTerminal];
	if (!t) return;
	const fitNow = () => { try { t.fit.fit(); } catch (_) { /* hidden while measuring */ } };
	fitNow();
	// Layout may not be final yet when the dock was just shown
	requestAnimationFrame(() => { fitNow(); requestAnimationFrame(fitNow); });
	t.term.focus();
}
window.addEventListener("resize", () => { if (!$("#dock").hidden) fitActive(); });

function activate(id) {
	activeTerminal = id;
	for (const [key, t] of Object.entries(terminals)) {
		t.pane.hidden = key !== id;
		t.tab.classList.toggle("active", key === id);
	}
	fitActive();
}

function connectTerminal(t) {
	const scheme = location.protocol === "https:" ? "wss://" : "ws://";
	const ws = new WebSocket(`${scheme}${location.host}/ws/term/${t.id}`);
	t.ws = ws;
	ws.onopen = () => {
		t.term.reset();
		ws.send(JSON.stringify({ t: "r", rows: t.term.rows, cols: t.term.cols }));
	};
	ws.onmessage = (event) => {
		const message = JSON.parse(event.data);
		if (message.o !== undefined) t.term.write(message.o);
		if (message.x !== undefined) {
			t.exited = true;
			updateSiteActivity();
			t.exitCode = message.x;
			if (t.autoClose && message.x === 0 && message.output_problem === false && t.remove) {
				t.remove();
				run(currentView === "sql" ? loadSqlConnections : loadSites);
				return;
			}
			t.term.write(`\r\n\x1b[33m[Process exited with code ${message.x}]\x1b[0m\r\n`);
			t.title.textContent += message.x === 0 && !message.output_problem ? " ✓" : " !";
			run(currentView === "sql" ? loadSqlConnections : loadSites).finally(() => {
				if (activeTerminal === t.id && !$("#dock").hidden && !document.querySelector("dialog[open]")) {
					requestAnimationFrame(fitActive);
				}
			});
		}
	};
	ws.onclose = () => {
		if (!t.exited && terminals[t.id]) setTimeout(() => terminals[t.id] && connectTerminal(t), 2000);
	};
}

function openTerminal(id, title, autoClose, sites = [], running = true, scope = "main", targets = []) {
	if (terminals[id]) return activate(id);
	const pane = el("div", { className: "pane", hidden: true });
	const titleNode = el("span", { textContent: title });
	const close = el("span", { className: "x", textContent: "✕", title: "Close (stops the process)" });
	const tab = el("div", { className: "tab", onclick: () => activate(id) }, titleNode, close);
	$("#tabs").append(tab);
	$("#panes").append(pane);

	const term = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000, fontFamily: "ui-monospace, Menlo, Consolas, monospace", theme: { background: "#000000" } });
	const fit = new FitAddon.FitAddon();
	term.loadAddon(fit);
	term.open(pane);
	const t = { id, term, fit, pane, tab, title: titleNode, taskTitle: title, sites, scope, targets: targets || [], exited: !running, ws: null, autoClose: !!autoClose, remove: null };
	terminals[id] = t;
	updateSiteActivity();

	// Enter closes finished terminals, including failed processes.
	term.attachCustomKeyEventHandler((event) => {
		if (t.exited && event.key === "Enter") {
			if (event.type === "keydown") closeDock();
			return false;
		}
		return true;
	});
	term.onData((data) => t.ws && t.ws.readyState === 1 && t.ws.send(JSON.stringify({ t: "i", d: data })));
	term.onResize(({ rows, cols }) => t.ws && t.ws.readyState === 1 && t.ws.send(JSON.stringify({ t: "r", rows, cols })));
	new ResizeObserver(() => { if (!pane.hidden && pane.clientHeight > 0) { try { fit.fit(); } catch (_) { /* ignore */ } } }).observe($("#panes"));
	setInterval(() => t.ws && t.ws.readyState === 1 && t.ws.send(JSON.stringify({ t: "p" })), 30000);

	t.remove = async () => {
		await run(() => api(`/api/term/${id}/close`, { json: {} }));
		t.exited = true;
		if (t.ws) t.ws.close();
		term.dispose(); tab.remove(); pane.remove();
		delete terminals[id];
		updateSiteActivity();
		activeTerminal = Object.keys(terminals)[0] || null;
		if (activeTerminal) activate(activeTerminal);
		updateTerminalState();
	};
	close.onclick = async (event) => {
		event.stopPropagation();
		if (!t.exited && !(await ask({ title: "Close terminal", message: "The process is still running. Close and stop it?", input: false, ok: "Stop and close", danger: true }))) return;
		await t.remove();
	};
	connectTerminal(t);
	updateTerminalState();
	activate(id);
}

async function openTask(payload) {
	const data = await api("/api/term", { json: payload });
	openTerminal(data.id, data.title, data.auto_close, data.sites, data.running, data.scope, data.targets);
	if (terminals[data.id]) {
		showDock();
		for (const dialog of document.querySelectorAll("dialog[open]")) {
			dialog.addEventListener("close", () => {
				if (activeTerminal === data.id && !document.querySelector("dialog[open]")) {
					requestAnimationFrame(fitActive);
				}
			}, { once: true });
		}
	}
}

async function restoreTerminals() {
	const { sessions } = await api("/api/term");
	for (const s of sessions) openTerminal(s.id, s.title, s.auto_close, s.sites, s.running, s.scope, s.targets);
	if (sessions.length) { for (const t of Object.values(terminals)) t.pane.hidden = true; activate(sessions[0].id); }
}

run(async () => {
	syncSiteForm();
	// The views are hidden until the address picks one, so a site's page does not show the list first
	try { await loadSites(); } finally { await route(); }
	await restoreTerminals();
	updateTerminalState();
});


// ------------------------------------------------------------------ settings menu
let sqlConnections = [];
let sqlToolsAvailable = false;
let sqlSnapshot = { databases: [], backups: [] };
let sqlLoadGeneration = 0;

function groupSqlBackups(backups) {
	const groups = new Map();
	for (const backup of backups) {
		if (!groups.has(backup.database_name)) groups.set(backup.database_name, []);
		groups.get(backup.database_name).push(backup);
	}
	for (const entries of groups.values()) {
		entries.sort((a, b) => b.finished.localeCompare(a.finished) || String(b.id).localeCompare(String(a.id)));
	}
	return [...groups.values()].sort((a, b) => a[0].database_name.localeCompare(b[0].database_name, undefined, { sensitivity: "base" }));
}

function sqlCell(label, props, ...children) {
	const cell = el("td", props, ...children);
	cell.dataset.label = label;
	return cell;
}

function sqlBackupRow(backup, allowDelete = true) {
	return el("tr", {}, sqlCell("Database", { textContent: backup.database_name }),
		sqlCell("Finished", { textContent: formatDate(backup.finished) }), sqlCell("Size", { className: "num", textContent: formatSqlSize(backup.size_mb) }),
		sqlCell("Server path", { textContent: backup.path }), sqlCell("Actions", {}, el("div", { className: "actions" }, el("button", {
			textContent: "Restore", disabled: backup.media_count !== 1,
			title: backup.error || (backup.media_count !== 1 ? "Striped or damaged backups are not supported" : "Restore this full backup"),
			onclick: () => run(() => sqlRestore(backup)),
		}), ...(allowDelete && sqlSnapshot.backup_file_access ? [el("button", {
			textContent: "Delete", onclick: () => run(async () => {
				if (!(await ask({ title: "Delete backup file?", message: `Delete the entire file ${backup.path}? All backup sets in this file will be removed. Type ${backup.database_name} to confirm.`, match: backup.database_name, danger: true, ok: "Delete" }))) return;
				await openTask({ kind: "sql_action", action: "delete_backup", connection: sqlSelected(),
					database: backup.database_name, backup_id: backup.id, confirmation: backup.database_name });
			}),
		})] : []))));
}

function sqlSelected() {
	const id = $("#sql-connection").value;
	if (!id) throw new Error("Choose a SQL connection.");
	return id;
}

async function loadSqlConnections(selected = $("#sql-connection").value) {
	const data = await api("/api/sql/connections");
	sqlConnections = data.connections;
	sqlToolsAvailable = data.tools_available === true;
	const select = $("#sql-connection");
	select.replaceChildren(el("option", { value: "", textContent: "Choose a connection..." }),
		...sqlConnections.map((item) => el("option", { value: item.id, textContent: item.name })));
	select.value = sqlConnections.some((item) => item.id === selected) ? selected : sqlConnections[0]?.id || "";
	await refreshSql();
}

async function refreshSql() {
	const id = sqlToolsAvailable ? $("#sql-connection").value : "";
	const selectedConnection = sqlConnections.find((item) => item.id === id);
	$("#sql-connection").title = selectedConnection ? `${selectedConnection.host}:${selectedConnection.port}` : "";
	const generation = ++sqlLoadGeneration;
	const hasConnections = sqlToolsAvailable && sqlConnections.length > 0;
	$("#sql-tools-required").hidden = sqlToolsAvailable;
	$("#sql-no-connections").hidden = !sqlToolsAvailable || hasConnections;
	$("#sql-heading").hidden = !hasConnections;
	$("#sql-connection-bar").hidden = !hasConnections;
	for (const key of ["sql-status", "sql-databases-panel"]) $("#" + key).hidden = !id;
	for (const key of ["sql-refresh", "sql-edit-connection", "sql-create", "sql-backup-all"]) {
		$("#" + key).hidden = !id;
		$("#" + key).disabled = !id;
	}
	$("#sql-backups-panel").hidden = true;
	$("#sql-backup-all").hidden = true;
	$("#sql-user-databases-table").hidden = true;
	$("#sql-databases").replaceChildren();
	$("#sql-system-rows").replaceChildren();
	$("#sql-system-databases").hidden = true;
	$("#sql-system-databases").open = false;
	$("#sql-backups").replaceChildren();
	sqlSnapshot = { databases: [], backups: [] };
	if (!id) {
		$("#sql-status").textContent = "Add a local or remote SQL connection to get started.";
		return;
	}
	$("#sql-status").textContent = "Loading databases and backup history...";
	try {
		const data = await api("/api/sql/" + encodeURIComponent(id));
		if (generation !== sqlLoadGeneration) return;
		sqlSnapshot = data;
		$("#sql-backups-panel").hidden = data.backups.length === 0;
		const hasUserDatabases = data.databases.some((database) => database.id > 4);
		$("#sql-backup-all").hidden = !hasUserDatabases;
		$("#sql-user-databases-table").hidden = !hasUserDatabases;
		const fileErrors = data.backups.filter((backup) => backup.error);
		$("#sql-status").textContent = `${data.databases.length} databases · ${data.backups.length} backups`;
		if (fileErrors.length) {
			$("#sql-status").textContent += " · " + fileErrors.map((backup) => `${backup.path}: ${backup.error}`).join(" · ");
		}
		const systemCount = data.databases.filter((database) => database.id <= 4).length;
		$("#sql-system-databases").hidden = !systemCount;
		$("#sql-system-summary").textContent = `System databases (${systemCount})`;
		for (const database of data.databases) {
			const actions = el("div", { className: "actions" });
			if (database.id > 4) {
				for (const [label, action] of [["Backup", "backup"], ["Copy", "copy"], ["Delete", "drop"]]) {
					actions.append(el("button", { textContent: label, onclick: () => run(() => sqlDatabaseAction(action, database)) }));
				}
			}
			actions.prepend(el("button", { textContent: "Query", onclick: () => openSqlQuery(database) }));
			const toggle = el("button", { type: "button", className: "sql-tables-toggle", title: "Show tables", ariaExpanded: "false",
				ariaLabel: `Show the tables of ${database.name}`, innerHTML: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>' });
			toggle.onclick = () => toggleSqlTables(toggle, database);
			// The whole row unfolds the database, except its buttons
			const row = el("tr", { className: "sql-database-row", onclick: (event) => {
				if (!event.target.closest("button, a, input, select") && !getSelection().toString()) toggle.click();
			} });
			row.append(sqlCell("Name", {}, el("div", { className: "sql-database-name" }, toggle, el("span", { textContent: database.name }))),
				sqlCell("Size", { className: "num", textContent: formatSqlSize(database.size_mb) }), sqlCell("State", {
					className: "sql-state-" + database.state.toLowerCase(),
					textContent: database.state,
				}),
				sqlCell("Recovery", { textContent: database.recovery }), sqlCell("Actions", {}, actions));
			$(database.id <= 4 ? "#sql-system-rows" : "#sql-databases").append(row);
		}
		for (const [latest, ...older] of groupSqlBackups(data.backups)) {
			$("#sql-backups").append(sqlBackupRow(latest));
			if (older.length) {
				const history = el("details", { className: "sql-backup-history" },
					el("summary", { textContent: `${older.length} older backup${older.length === 1 ? "" : "s"} for ${latest.database_name}` }),
					el("table", {},
						el("thead", {}, el("tr", {}, ...["Database", "Finished", "Size", "Server path", "Actions"].map((label) => el("th", { textContent: label, className: label === "Size" ? "num" : "" })))),
						el("tbody", {}, ...older.map((backup) => sqlBackupRow(backup, false)))));
				$("#sql-backups").append(el("tr", {}, el("td", { colSpan: 5 }, history)));
			}
		}
		decorateSql();
	} catch (error) {
		if (generation !== sqlLoadGeneration) return;
		$("#sql-status").textContent = error.message;
		for (const key of ["sql-create", "sql-backup-all"]) $("#" + key).disabled = true;
		throw error;
	}
}

async function sqlDatabaseAction(action, database) {
	const connection = sqlSelected();
	const payload = { kind: "sql_action", connection, action, database: database.name };
	if (action === "copy") {
		const values = await askSqlDatabase(database.name);
		if (!values) return;
		Object.assign(payload, values);
	} else if (action === "drop") {
		const confirmation = await ask({ title: "Delete database?", message: `This permanently deletes ${database.name} and disconnects all sessions. Shared and administrator logins are kept. Type ${database.name} to confirm.`, match: database.name, danger: true, ok: "Delete", loginCheckbox: true,
			checkbox: sqlSnapshot.backup_file_access ? "Also delete backup files for this database" : null });
		if (!confirmation) return;
		payload.confirmation = confirmation.value;
		payload.delete_login = confirmation.deleteLogin;
		payload.delete_backups = sqlSnapshot.backup_file_access ? confirmation.checked : false;
	} else if (!(await ask({ title: "Backup database?", message: `Create a full copy-only backup of ${database.name} in the connection's server-side backup directory.`, input: false, ok: "Backup" }))) return;
	await openTask(payload);
}

// One dialog asks everything: the name to restore as, and a new login for a new database,
// or, when the name is an existing database, the confirmation that it is overwritten
async function sqlRestore(backup) {
	const connection = sqlSelected();
	const values = await askSqlRestore(backup);
	if (!values) return;
	await openTask({ kind: "sql_action", connection, action: "restore", database: backup.database_name, backup_id: backup.id, ...values });
}

function askSqlRestore(backup) {
	const dialog = $("#dlg-sql-restore");
	const form = $("#form-sql-restore");
	const error = $("#sql-restore-error");
	form.reset();
	error.textContent = "";
	$("#sql-restore-description").textContent = `From ${backup.path}. SQL Server must still be able to read this file.`;
	form.elements.database.value = backup.database_name;
	let loginEdited = false;
	const existing = () => {
		const name = form.elements.database.value.trim().toLowerCase();
		return sqlSnapshot.databases.find((item) => item.name.toLowerCase() === name);
	};
	const update = () => {
		const match = existing();
		$("#sql-restore-login").hidden = $("#sql-restore-login").disabled = !!match;
		$("#sql-restore-overwrite").hidden = $("#sql-restore-overwrite").disabled = !match;
		if (match) $("#sql-restore-overwrite-text").textContent = `All data in ${match.name} is replaced and its sessions are disconnected. Its logins are kept. Take a backup first.`;
		const submit = $("#sql-restore-submit");
		submit.textContent = match ? "Overwrite and restore" : "Restore and create login";
		submit.classList.toggle("danger", !!match);
		if (!loginEdited) form.elements.login.value = form.elements.database.value.trim();
	};
	form.elements.database.oninput = update;
	form.elements.login.oninput = () => { loginEdited = true; };
	update();
	return new Promise((resolve) => {
		let result = null;
		form.onsubmit = (event) => {
			event.preventDefault();
			const name = form.elements.database.value.trim();
			const match = existing();
			if (match) {
				// The server checks the confirmation against the name it restores as, as typed above
				if (form.elements.match.value.trim() !== name) {
					error.textContent = `Type ${name} to confirm.`;
					return;
				}
				result = { target: name, overwrite: true, confirmation: name };
			} else {
				if (form.elements.password.value !== form.elements.confirmation.value) {
					error.textContent = "Passwords do not match.";
					return;
				}
				result = { target: name, overwrite: false, confirmation: "", login: form.elements.login.value.trim(), login_password: form.elements.password.value };
			}
			dialog.close();
		};
		$("#sql-restore-cancel").onclick = () => dialog.close();
		dialog.onclose = () => { form.reset(); error.textContent = ""; resolve(result); };
		dialog.showModal();
		form.elements.database.select();
	});
}

function askSqlDatabase(source = null) {
	const dialog = $("#dlg-sql-database");
	const form = $("#form-sql-database");
	const copying = source !== null;
	form.reset();
	$("#sql-database-icon").replaceChildren(svgIcon(copying ? "sync" : "server"));
	$("#sql-database-title").textContent = copying ? "Copy database" : "Create database";
	$("#sql-database-description").textContent = copying
		? `Back up ${source} and restore to a new database. Existing database users remain in the backup; server logins are not copied.`
		: "Creates a database only, not a new SQL login.";
	$("#sql-database-submit").textContent = copying ? "Copy and create login" : "Create database";
	const loginFields = $("#sql-database-login");
	loginFields.hidden = !copying;
	loginFields.disabled = !copying;
	const error = $("#sql-database-error");
	error.textContent = "";
	let loginEdited = false;
	form.elements.database.oninput = () => {
		if (!loginEdited) form.elements.login.value = form.elements.database.value;
	};
	form.elements.login.oninput = () => { loginEdited = true; };
	return new Promise((resolve) => {
		let result = null;
		form.onsubmit = (event) => {
			event.preventDefault();
			if (copying && form.elements.password.value !== form.elements.confirmation.value) {
				error.textContent = "Passwords do not match.";
				return;
			}
			const name = form.elements.database.value.trim();
			result = copying
				? { target: name, login: form.elements.login.value.trim(), login_password: form.elements.password.value }
				: { database: name };
			dialog.close();
		};
		$("#sql-database-cancel").onclick = () => dialog.close();
		dialog.onclose = () => { form.reset(); error.textContent = ""; resolve(result); };
		dialog.showModal();
	});
}

function decorateSql() {
	const styles = {
		"Add connection": ["server", "blue"], "Remove connection": ["trash", "red"],
		"Install SQL tools": ["download", "blue"],
		"Edit connection": ["server", "blue"],
		"Create database": ["server", "green"], "Backup": ["download", "blue"],
		"Backup all": ["download", "blue"], "Copy": ["sync", "purple"],
		"Restore": ["upload", "amber"], "Refresh": ["sync", "blue"],
	};
	for (const button of $("#view-sql").querySelectorAll("button:not([data-tone])")) {
		const style = styles[button.textContent.trim()];
		if (!style) continue;
		button.dataset.tone = style[1];
		button.prepend(svgIcon(style[0]));
	}
	decorate($("#view-sql"));
}

$("#switch-sites").onclick = () => go("#sites");
$("#switch-sql").onclick = () => go("#sql");
// With SQL Server as the start page, it is the home: its page has no link back to the sites
function applyStartView() {
	const sql = startView === "sql";
	const brand = document.querySelector(".brand");
	brand.href = sql ? "#sql" : "#sites";
	brand.title = "Reload";
}
// The logo leads from a site back to the overview, and reloads the overview or SQL Server where you are.
// Opened in a new tab, its address leads to the start page.
document.querySelector(".brand").onclick = (event) => {
	event.preventDefault();
	run(async () => {
		if (currentView !== "sql") await loadSites();
		if (currentView === "site") go("#sites");
		else await route();
	});
};
applyStartView();
// Preferences: where the admin opens, and the keep-alive of the sites
const preferencesDialog = $("#dlg-preferences");
const preferencesForm = $("#form-preferences");
let keepAliveInstalled = false;
$("#menu-preferences").onclick = () => run(async () => {
	$("#user-menu").hidden = true;
	preferencesForm.reset();
	preferencesForm.elements.start_view.value = startView;
	const keepAlive = await api("/api/keep-alive");
	keepAliveInstalled = keepAlive.installed;
	preferencesForm.elements.keep_alive.checked = keepAlive.enabled;
	preferencesForm.elements.keep_alive_interval.value = keepAlive.interval_minutes;
	for (const name of ["keep_alive", "keep_alive_interval"]) preferencesForm.elements[name].disabled = !keepAlive.installed;
	syncKeepAliveInterval();
	$("#preferences-keep-alive-hint").textContent = keepAlive.installed
		? "From 1 to 1440 minutes. It does not restart services or prevent Boot failed."
		: "The keep-alive cron job is missing. Run apache/init.sh on this server to install it.";
	preferencesDialog.showModal();
	document.activeElement?.blur();
});
$("#preferences-cancel").onclick = () => preferencesDialog.close();
// The interval: a quick choice, or any number of minutes. It is only in play while keep-alive is on.
function syncKeepAliveInterval() {
	const minutes = Number(preferencesForm.elements.keep_alive_interval.value);
	const on = preferencesForm.elements.keep_alive.checked && !preferencesForm.elements.keep_alive.disabled;
	$("#keep-alive-interval").classList.toggle("off", !on);
	for (const button of $("#keep-alive-interval").querySelectorAll("button")) {
		button.classList.toggle("active", Number(button.dataset.minutes) === minutes);
		button.disabled = !on;
	}
	preferencesForm.elements.keep_alive_interval.disabled = !on;
}
for (const button of $("#keep-alive-interval").querySelectorAll("button")) {
	button.onclick = () => {
		preferencesForm.elements.keep_alive_interval.value = button.dataset.minutes;
		syncKeepAliveInterval();
	};
}
preferencesForm.elements.keep_alive_interval.oninput = syncKeepAliveInterval;
preferencesForm.elements.keep_alive.onchange = syncKeepAliveInterval;
preferencesForm.onsubmit = (event) => {
	event.preventDefault();
	const next = preferencesForm.elements.start_view.value;
	const interval = Number(preferencesForm.elements.keep_alive_interval.value) || 5;
	run(async () => {
		if (keepAliveInstalled) {
			if (!Number.isInteger(interval) || interval < 1 || interval > 1440) throw new Error("The interval must be a whole number from 1 to 1440 minutes.");
			await api("/api/keep-alive", { method: "PUT", json: { enabled: preferencesForm.elements.keep_alive.checked, interval_minutes: interval } });
		}
		if (next !== startView) {
			await api("/api/preferences", { method: "PUT", json: { start_view: next } });
			startView = next;
			applyStartView();
		}
		preferencesDialog.close();
		toast("Preferences saved", "ok");
	});
};

// Scripts typed here run as the connection's login, against the chosen database
const sqlQueryDialog = $("#dlg-sql-query");
// Unfold a database into the list of its tables. A table opens the query dialog with its first rows.
async function toggleSqlTables(toggle, database) {
	const row = toggle.closest("tr");
	const open = toggle.getAttribute("aria-expanded") === "true";
	if (open) {
		if (row.nextElementSibling?.classList.contains("sql-tables-row")) row.nextElementSibling.remove();
		toggle.setAttribute("aria-expanded", "false");
		return;
	}
	toggle.setAttribute("aria-expanded", "true");
	const body = el("div", { className: "sql-tables muted", textContent: "Loading tables…" });
	const detail = el("tr", { className: "sql-tables-row" }, el("td", { colSpan: 5 }, body));
	row.after(detail);
	try {
		const data = await api(`/api/sql/${encodeURIComponent($("#sql-connection").value)}/tables?database=${encodeURIComponent(database.name)}`);
		body.classList.remove("muted");
		if (!data.tables.length) {
			body.replaceChildren(el("p", { className: "muted", textContent: "No tables." }));
			return;
		}
		const quote = (name) => "[" + name.replaceAll("]", "]]") + "]";
		body.replaceChildren(el("div", { className: "sql-tables-count muted", textContent: `${data.tables.length} table${data.tables.length === 1 ? "" : "s"}` }),
			el("ul", {}, ...data.tables.map((table) => el("li", {}, el("button", {
				type: "button", title: "Show the first 100 rows",
				onclick: () => openSqlQuery(database, `SELECT TOP 100 * FROM ${quote(table.schema)}.${quote(table.name)}`),
			}, el("span", { className: "sql-table-name" }, ...(table.schema === "dbo" ? [] : [el("span", { className: "muted", textContent: table.schema + "." })]), table.name),
				el("span", { className: "sql-table-meta muted", textContent: `${Number(table.rows).toLocaleString()} rows · ${formatSqlSize(table.size_mb)}` }))))));
	} catch (error) {
		body.replaceChildren(el("p", { className: "sql-tables-error", textContent: error.message || "Could not read the tables." }));
	}
}

function openSqlQuery(database, script) {
	// Another database starts empty, so a script is never run against the wrong one by mistake
	const connection = $("#sql-connection").value;
	if (sqlQueryDialog.dataset.database !== database.name || sqlQueryDialog.dataset.connection !== connection) {
		$("#sql-query-text").value = "";
		$("#sql-query-output").replaceChildren();
		$("#sql-query-output").classList.remove("sql-query-error");
	}
	sqlQueryDialog.dataset.database = database.name;
	sqlQueryDialog.dataset.connection = connection;
	$("#sql-query-title").textContent = `Query ${database.name}`;
	$("#sql-query-status").textContent = "";
	if (!sqlQueryDialog.open) sqlQueryDialog.showModal();
	$("#sql-query-text").focus();
	if (script) {
		$("#sql-query-text").value = script;
		runSqlQuery();
	}
}
const sqlNumber = /^-?\d+(\.\d+)?$/;
const maxShownRows = 2000;
// Each result set as a table, with messages such as PRINT as text in between
function showSqlResults(output, data) {
	const parts = [];
	for (const result of data.results || []) {
		const shown = result.rows.slice(0, maxShownRows);
		const numeric = result.columns.map((_, index) => shown.length > 0 && shown.every((row) => row[index] === "NULL" || sqlNumber.test(row[index])));
		const table = el("table", { className: "sql-result-table" },
			el("thead", {}, el("tr", {}, ...result.columns.map((column, index) => el("th", { textContent: column || "(no name)", className: numeric[index] ? "num" : "" })))),
			el("tbody", {}, ...shown.map((row) => el("tr", {}, ...row.map((value, index) => el("td", {
				textContent: value, title: value.length > 60 ? value : "",
				className: (value === "NULL" ? "sql-null " : "") + (numeric[index] ? "num" : ""),
			}))))));
		const count = result.rows.length === shown.length ? `${result.rows.length} row${result.rows.length === 1 ? "" : "s"}`
			: `First ${shown.length} of ${result.rows.length} rows`;
		parts.push(el("section", { className: "sql-result" }, el("div", { className: "sql-result-count", textContent: count }),
			el("div", { className: "sql-result-scroll" }, table)));
	}
	for (const message of data.messages || []) parts.push(el("pre", { className: "sql-result-message", textContent: message }));
	if (!parts.length) parts.push(el("p", { className: "muted sql-result-empty", textContent: "The script ran. It returned no rows." }));
	output.replaceChildren(...parts);
	output.scrollTop = 0;
}

async function runSqlQuery() {
	const button = $("#sql-query-run");
	if (button.disabled) return;
	button.disabled = true;
	$("#sql-query-status").textContent = "Running…";
	try {
		const response = await fetch(`/api/sql/${encodeURIComponent(sqlQueryDialog.dataset.connection)}/query`, {
			method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
			body: JSON.stringify({ database: sqlQueryDialog.dataset.database, query: $("#sql-query-text").value }),
		});
		const data = await response.json().catch(() => ({}));
		const output = $("#sql-query-output");
		output.classList.toggle("sql-query-error", !response.ok);
		if (response.ok) showSqlResults(output, data);
		else output.replaceChildren(el("pre", { textContent: data.error || data.message || "The script failed." }));
		const rows = (data.results || []).reduce((sum, result) => sum + result.rows.length, 0);
		$("#sql-query-status").textContent = response.ok
			? `Done in ${data.seconds} s${data.results?.length ? ` · ${rows} row${rows === 1 ? "" : "s"}` : ""}` : "Failed";
	} catch (error) {
		$("#sql-query-status").textContent = error.message;
	} finally {
		button.disabled = false;
	}
}
$("#sql-query-run").onclick = runSqlQuery;
$("#sql-query-close").onclick = () => sqlQueryDialog.close();
$("#sql-query-text").onkeydown = (event) => {
	if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
		event.preventDefault();
		runSqlQuery();
	}
};
$("#sql-refresh").onclick = () => run(refreshSql);
$("#sql-connection").onchange = () => run(refreshSql);
$("#sql-create").onclick = () => run(async () => {
	const connection = sqlSelected();
	const values = await askSqlDatabase();
	if (values) await openTask({ kind: "sql_action", connection, action: "create", ...values });
});
$("#sql-backup-all").onclick = () => run(async () => {
	const connection = sqlSelected();
	if (await ask({ title: "Backup all databases?", message: "Create full copy-only backups of all online user databases on this connection. System databases are excluded.", input: false, ok: "Backup all" })) {
		await openTask({ kind: "sql_action", connection, action: "backup_all" });
	}
});
let editingSqlConnection = null;
$("#sql-remove-connection").onclick = () => run(async () => {
	const id = editingSqlConnection;
	if (!id) throw new Error("Select a SQL connection to remove.");
	if (!(await ask({ title: "Remove connection?", message: "Remove the saved connection and credentials from this admin server. Databases and backups are not deleted.", input: false, ok: "Remove", danger: true }))) return;
	await api("/api/sql/connections", { method: "DELETE", json: { id } });
	$("#dlg-sql-connection").close();
	await loadSqlConnections();
});
function openSqlConnection(connection = null) {
	if (!sqlToolsAvailable) throw new Error("Install SQL command-line tools on the admin server first.");
	editingSqlConnection = connection?.id || null;
	const form = $("#form-sql-connection");
	form.reset();
	if (connection) {
		for (const key of ["name", "host", "port", "username", "backup_directory"]) form.elements[key].value = connection[key];
		form.elements.trust_certificate.checked = connection.trust_certificate;
	}
	form.elements.ssh_enabled.checked = connection?.ssh_enabled || false;
	for (const key of ["ssh_port", "ssh_username"]) {
		form.elements[key].value = connection?.[key] || (key === "ssh_port" ? 22 : "");
	}
	updateSqlSshFields();
	form.elements.password.required = !connection;
	$("#sql-password-help").hidden = !connection;
	$("#sql-remove-connection").hidden = !connection;
	$("#sql-connection-title").textContent = connection ? "Edit SQL connection" : "Add SQL connection";
	$("#dlg-sql-connection").showModal();
}
function updateSqlSshFields() {
	const form = $("#form-sql-connection");
	const enabled = form.elements.ssh_enabled.checked;
	$("#sql-ssh-fields").hidden = !enabled;
	$("#sql-ssh-fields").disabled = !enabled;
	form.elements.ssh_host.value = form.elements.host.value;
}
$("#form-sql-connection").elements.ssh_enabled.onchange = updateSqlSshFields;
$("#form-sql-connection").elements.host.oninput = updateSqlSshFields;
$("#sql-add-connection").onclick = () => openSqlConnection();
$("#sql-empty-add-connection").onclick = () => openSqlConnection();
$("#sql-install-tools").onclick = () => run(() => openTask({ kind: "install_sql_tools" }));
$("#sql-edit-connection").onclick = () => run(() => {
	const connection = sqlConnections.find((item) => item.id === sqlSelected());
	if (!connection) throw new Error("SQL connection no longer exists.");
	openSqlConnection(connection);
});
$("#dlg-sql-connection").addEventListener("close", () => {
	$("#form-sql-connection").reset();
	editingSqlConnection = null;
});
$("#sql-connection-cancel").onclick = () => $("#dlg-sql-connection").close();
$("#form-sql-connection").onsubmit = (event) => {
	event.preventDefault();
	const form = event.target;
	const data = Object.fromEntries(new FormData(form));
	data.port = Number(data.port);
	data.trust_certificate = form.elements.trust_certificate.checked;
	data.ssh_enabled = form.elements.ssh_enabled.checked;
	if (data.ssh_enabled) data.ssh_port = Number(data.ssh_port);
	if (editingSqlConnection) data.id = editingSqlConnection;
	run(async () => {
		const saved = await api("/api/sql/connections", { method: data.id ? "PUT" : "POST", json: data });
		form.reset();
		$("#dlg-sql-connection").close();
		await loadSqlConnections(saved.id);
	});
};
decorateSql();

const settingsMenu = $("#settings-menu");
let systemUpdating = false;
async function runSystemUpdate(existing = null) {
	if (systemUpdating) return;
	systemUpdating = true;
	const overlay = $("#system-update-overlay");
	$("#system-update-title").textContent = "Updating..";
	$("#system-update-message").textContent = "Please wait. The page will reload automatically when the update completes.";
	$("#system-update-step").hidden = true;
	// From which version to which, also when the page was reloaded during the update
	const showVersions = (state) => {
		if (!state || (!state.from && !state.to)) return;
		$("#system-update-title").textContent = state.to ? `Updating to ${state.to}` : "Updating to the latest";
		$("#system-update-versions-line").textContent = `${state.from ? "v" + state.from : "Installed version"} → ${state.to || "the latest on main"}`;
		$("#system-update-versions-line").hidden = false;
	};
	$("#system-update-output").hidden = true;
	$("#system-update-close").hidden = true;
	$(".update-spinner").hidden = false;
	overlay.showModal();
	try {
		const job = existing || await api("/api/system-update/run", { json: {} });
		showVersions(job);
		const deadline = Date.now() + 35 * 60 * 1000;
		while (Date.now() < deadline) {
			await new Promise((resolve) => setTimeout(resolve, 2000));
			let response;
			try {
				response = await fetch("/api/system-update/run", { cache: "no-store", signal: AbortSignal.timeout(5000) });
			} catch (error) {
				if (!(error instanceof TypeError) && error.name !== "TimeoutError") throw error;
				continue;
			}
			if (response.status === 401 || response.redirected) throw new Error("Your session expired. Reload the page and log in to check the update.");
			if ([502, 503, 504].includes(response.status)) continue;
			if (!response.ok) throw new Error("Could not check update status: HTTP " + response.status);
			const state = await response.json();
			if (state.id !== job.id) throw new Error("Update status changed unexpectedly. Reload the page to check it.");
			showVersions(state);
			if (state.step) {
				$("#system-update-step").textContent = state.step;
				$("#system-update-step").hidden = false;
			}
			if (state.status === "success") { location.reload(); return; }
			if (state.status === "error") {
				$("#system-update-output").textContent = state.output || "";
				$("#system-update-output").hidden = !state.output;
				throw new Error(state.error || "Update failed.");
			}
		}
		throw new Error("The update has not confirmed completion. Reload the page to check its status; do not start another update.");
	} catch (error) {
		$("#system-update-title").textContent = "Update not completed";
		$("#system-update-message").textContent = error.message;
		$(".update-spinner").hidden = true;
		$("#system-update-close").hidden = false;
		console.error(error);
	} finally {
		systemUpdating = false;
	}
}
$("#system-update-overlay").addEventListener("cancel", (event) => event.preventDefault());
$("#system-update-close").onclick = () => $("#system-update-overlay").close();
async function checkSystemUpdate(refresh = false) {
	const result = await api("/api/system-update" + (refresh ? "?refresh=1" : ""));
	const badge = $("#system-update-badge");
	const available = result.status === "available";
	const unknown = result.status === "unknown";
	badge.hidden = !available && !unknown;
	badge.textContent = unknown ? "Version unknown" : "Update available";
	badge.classList.toggle("system-update-available", available);
	// At the bottom of the menu: the version, and an update when there is one
	$("#settings-update").hidden = !available && !unknown;
	$("#settings-uptodate").hidden = result.status !== "current";
	$("#settings-update").replaceChildren(svgIcon("sync"), unknown ? "Initialize / update" : "Update");
	const installedText = result.installed_version ? `v${result.installed_version}` : shortCommit(result.installed);
	const latestText = result.mode === "release" ? `v${result.latest_version}` : `main (${shortCommit(result.latest)})`;
	$("#btn-settings").title = unknown ? "Installed version unknown. Use Initialize / update to install the latest version and record it."
		: result.status === "error" ? result.error
		: available ? `Update available · Installed: ${installedText} · New: ${latestText}` : "Settings";
	$("#settings-version").textContent = result.installed_version ? `v${result.installed_version}` : installedText || "";
	if (available) $("#settings-update").replaceChildren(svgIcon("sync"), result.mode === "release" ? `Update to ${latestText}` : "Update to the latest");
	if (result.status === "error") throw new Error(result.error);
}
$("#btn-settings").onclick = (event) => {
	event.stopPropagation();
	$("#user-menu").hidden = true;
	settingsMenu.hidden = !settingsMenu.hidden;
	if (!settingsMenu.hidden) run(() => checkSystemUpdate(true));
};
document.addEventListener("click", () => { settingsMenu.hidden = true; });
$("#settings-restart-apache").onclick = () => run(async () => {
	if (await ask({ title: "Restart Apache?", message: "All sites on this server are briefly unavailable.", input: false, ok: "Restart", danger: true })) await openTask({ kind: "apache", tool: "restart" });
});
$("#settings-admin-site").onclick = () => { const admin = allSites.find((s) => s.admin); if (admin) go(siteLink(admin.name)); };
$("#settings-apache-test").onclick = () => run(() => openTask({ kind: "apache", tool: "configtest" }));
// Release notes are Markdown: headings, lists and paragraphs, built as text so nothing in them runs
function markdownBlock(text) {
	const box = el("div", { className: "release-notes" });
	let list = null;
	const plain = (line) => line.replace(/\*\*(.+?)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
	for (const raw of String(text || "").split("\n")) {
		const line = raw.trimEnd();
		const heading = line.match(/^#{1,6}\s+(.*)$/);
		const item = line.match(/^(\s*)[-*]\s+(.*)$/);
		if (item) {
			if (!list) box.append(list = el("ul"));
			list.append(el("li", { className: item[1].length >= 2 ? "nested" : "", textContent: plain(item[2]) }));
			continue;
		}
		list = null;
		if (heading) box.append(el("h4", { textContent: plain(heading[1]) }));
		else if (line.trim()) box.append(el("p", { textContent: plain(line) }));
	}
	return box;
}
const shortCommit = (value) => (value || "").slice(0, 7);
// What an update brings, before it starts: the versions, the release notes or the latest commit, and a link
async function openSystemUpdateDialog() {
	const result = await api("/api/system-update?refresh=1");
	const releases = result.mode === "release";
	const from = result.installed_version ? `v${result.installed_version}` : result.installed ? shortCommit(result.installed) : "an unknown version";
	const to = releases ? `v${result.latest_version}` : `the latest on main (${shortCommit(result.latest)})`;
	$("#system-update-versions").textContent = `Installed: ${from} → ${to}`;
	const notes = $("#system-update-notes");
	notes.replaceChildren();
	if (releases) {
		for (const release of result.releases || []) {
			notes.append(el("section", { className: "system-update-release" },
				el("div", { className: "system-update-release-head" },
					el("strong", { textContent: release.name }),
					release.date ? el("span", { className: "muted", textContent: formatDate(release.date) }) : "",
					release.url ? el("a", { href: release.url, target: "_blank", rel: "noopener noreferrer", textContent: "On GitHub ↗" }) : ""),
				markdownBlock(release.notes || (result.notes_unavailable
					? "The release notes could not be read from GitHub right now. They are on GitHub." : "No notes."))));
		}
	} else if (result.latest_commit) {
		const commit = result.latest_commit;
		notes.append(el("section", { className: "system-update-release" },
			el("div", { className: "system-update-release-head" },
				el("strong", { textContent: "Latest change" }),
				commit.date ? el("span", { className: "muted", textContent: formatDate(commit.date) }) : "",
				commit.url ? el("a", { href: commit.url, target: "_blank", rel: "noopener noreferrer", textContent: "Commit ↗" }) : ""),
			el("p", { className: "system-update-subject", textContent: commit.subject }),
			commit.body ? markdownBlock(commit.body) : ""));
	}
	if (!notes.children.length) notes.append(el("p", { className: "muted", textContent: "No release notes for this update." }));
	const changes = $("#system-update-changes");
	changes.hidden = !result.compare_url;
	changes.href = result.compare_url || "#";
	const dialog = $("#dlg-system-update");
	const answer = await new Promise((resolve) => {
		dialog.querySelector("form").returnValue = "";
		dialog.returnValue = "";
		dialog.onclose = () => resolve(dialog.returnValue === "ok");
		dialog.showModal();
		// Update has the focus, so Enter or Space starts it
		dialog.querySelector('button[type="submit"]').focus();
	});
	if (answer) await runSystemUpdate();
}
$("#system-update-cancel").onclick = () => $("#dlg-system-update").close();
$("#settings-update").onclick = () => run(openSystemUpdateDialog);
$("#settings-update").dataset.tone = "purple";
$("#settings-update").prepend(svgIcon("sync"));

decorate($("#settings-menu"));
// Restarting Apache takes every site down for a moment: it stands last, in red
$("#settings-restart-apache").dataset.tone = "red";
// The account menu: every item with its icon
for (const [id, icon, tone] of [["menu-preferences", "sliders", "purple"], ["menu-profile", "user", "blue"], ["menu-logout", "logout", "red"]]) {
	$("#" + id).dataset.tone = tone;
	$("#" + id).prepend(svgIcon(icon));
}
decorate($("#view-sites .toolbar"));
decorate($("#view-files"));
run(() => checkSystemUpdate(true));
run(async () => {
	const state = await api("/api/system-update/run");
	if (state.status === "running") await runSystemUpdate(state);
});
setInterval(() => { if (!document.hidden && !systemUpdating) run(checkSystemUpdate); }, 60000);
document.addEventListener("visibilitychange", () => {
	if (!document.hidden && !systemUpdating) run(() => checkSystemUpdate(true));
});
