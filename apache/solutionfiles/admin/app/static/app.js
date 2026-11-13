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

// Who is logged in and what they may do (/api/me). The server checks every request; the page only leaves out
// what cannot be used. An administrator may do everything.
let currentUser = { admin: document.querySelector("meta[name=role]")?.content !== "user", sites: {}, connections: {} };
const envKeyOf = (env) => (env.key === "main" ? "main" : env.target || "prod");
function canSee(siteName, envKey) {
	if (currentUser.admin) return true;
	const grant = currentUser.web && currentUser.sites[siteName];
	if (!grant) return false;
	return grant.envs.includes(envKey) || (grant.new_envs && envKey !== "main" && !(grant.known || []).includes(envKey));
}
function can(siteName, envKey, action) {
	if (currentUser.admin) return true;
	return canSee(siteName, envKey) && (currentUser.sites[siteName].actions || []).includes(action);
}
function canAnywhere(site, action) {
	return currentUser.admin || (site.environments || []).some((env) => can(site.name, envKeyOf(env), action));
}
function canSql(connection, database, action) {
	if (currentUser.admin) return true;
	const grant = currentUser.sql && currentUser.connections[connection];
	if (!grant) return false;
	const databases = grant.databases || [];
	const inScope = database == null || databases.includes("*") || databases.includes(database);
	return inScope && (action === "view" || (grant.actions || []).includes(action));
}
// The permission a button on a site's page needs, by its label; "admin" is an administrator's alone
const buttonPermission = {
	"Publish": "publish", "Publish all": "publish", "Publish environments": "publish",
	"Clear logs": "clear_logs", "Sync data…": "sync_data", "Sync settings…": "sync_settings", "Sync setup…": "sync_any",
	"Fix permissions": "files", "Change bindings": "bindings", "Issue certificate": "bindings", "Change environment": "bindings",
	"Check service": "admin", "Edit service": "admin", "Edit config": "admin", "Edit web.config": "files", "Edit appsettings.json": "appsettings",
	"Stop": "service", "Start": "service", "Restart": "service", "Logs": "logs", "Shell": "admin", "Files": "files",
	"Copy site": "copy_site", "Delete site": "delete", "Remove environment": "delete", "Delete everything": "delete_all",
	"Migrate…": "admin", "Move out…": "admin", "Set up site…": "admin", "Move under main site": "admin", "Add environment": "create_env",
	"Check": "packages", "Update all": "packages", "Update": "packages", "Add": "packages", "Remove": "packages", "Run custom script": "scripts",
};
function allowedButton(label, site, envKey) {
	const need = buttonPermission[label];
	if (!need || currentUser.admin) return true;
	if (need === "admin") return false;
	if (need === "sync_any") return can(site.name, envKey, "sync_data") || can(site.name, envKey, "sync_settings");
	if (need === "copy_site") return currentUser.create_sites && can(site.name, envKey, "files");
	if (need === "delete_all") return (site.environments || []).every((env) => can(site.name, envKeyOf(env), "delete"));
	if (envKey) return can(site.name, envKey, need);
	return canAnywhere(site, need);
}
// Leave out the buttons in a part of the page the user cannot use, and rows that are empty then
function pruneButtons(root, site, envKey = null) {
	if (currentUser.admin || !root) return;
	for (const button of root.querySelectorAll("button")) {
		const label = button.textContent.trim().replace(/^\s+/, "");
		const own = button.closest("[data-env]")?.dataset.env || envKey;
		if (!allowedButton(label, site, own)) button.remove();
	}
	for (const row of root.querySelectorAll(".actions, .script-menu")) {
		if (!row.querySelector("button, a")) row.remove();
	}
}

// What the page shows of the user's access: the parts, and what they may make
function applyAccess() {
	$("#switch-sites").hidden = !currentUser.web;
	$("#switch-sql").hidden = !currentUser.sql;
	$("#switch-sites").closest("nav").hidden = !(currentUser.web && currentUser.sql);
	$("#btn-new-site").hidden = !currentUser.create_sites;
	$("#sql-add-connection").hidden = !currentUser.manage_connections;
	// A user with only one of the two has nothing to choose in Preferences
	$("#menu-preferences").hidden = !currentUser.admin && !(currentUser.web && currentUser.sql);
}
// An administrator can change a user's access while their page is open: it is read again now and then, and the page
// follows at once, out of a part the user no longer has and with the buttons they now have
let accessSnapshot = "";
async function refreshAccess() {
	if (currentUser.admin || document.hidden) return;
	let latest;
	try { latest = await api("/api/me"); } catch (error) { return; }
	const snapshot = JSON.stringify(latest);
	if (snapshot === accessSnapshot) return;
	const first = !accessSnapshot;
	accessSnapshot = snapshot;
	currentUser = latest;
	if (first) return;
	applyAccess();
	const onSql = location.hash.startsWith("#sql");
	if ((onSql && !currentUser.sql) || (!onSql && !currentUser.web)) {
		await route();
	} else if (onSql) {
		await loadSqlConnections();
	} else {
		await loadSites();
	}
}
setInterval(() => run(refreshAccess), 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) run(refreshAccess); });

// ------------------------------------------------------------------ DNS help
// Under a binding: the record to make for it, with copy buttons, and whether the name points here already.
// It points at this server (its public address when one is set), or at the address given, as a remote server's.
let serverAddress = null;
async function loadServerAddress() {
	if (!serverAddress) serverAddress = await api("/api/server-address");
	return serverAddress;
}
// A DNS record to make, with a button that copies its value
function dnsRecord(type, name, value) {
	const button = el("button", { type: "button", className: "dns-copy", title: "Copy " + value, ariaLabel: "Copy " + value });
	button.append(svgIcon("copy"));
	button.onclick = () => run(async () => {
		// Copied from inside its card, so it works in an open dialog too
		await copyText(value, button.closest(".dns-card") || document.body);
		button.classList.add("copied");
		setTimeout(() => button.classList.remove("copied"), 1200);
	});
	return el("div", { className: "dns-record" },
		el("span", { className: "dns-type", textContent: type }), el("code", { textContent: name }),
		el("span", { className: "dns-arrow", textContent: "→" }), el("code", { className: "dns-value", textContent: value }), button);
}

// One row per name with where it points, and the record to make under each name that does not point here
function dnsNameRows(names, answers, address, expect) {
	return names.map((name, index) => {
		const answer = answers[index];
		const [kind, text] = answer?.local ? ["muted", "Local name"] : !answer ? ["muted", "Could not check"] : answer.here ? ["ok", answer.proxy ? "Via " + answer.proxy : "Points here"]
			: answer.addresses.length ? ["warn", "Points to " + answer.addresses[0]] : ["muted", "No record yet"];
		const status = el("span", { className: "dns-status " + kind, textContent: text });
		if (answer?.addresses?.length > 1) status.title = answer.addresses.join(", ");
		const row = el("div", { className: "dns-check-row" }, el("code", { textContent: name }), status);
		if (answer?.here || answer?.local) return row;
		return el("div", { className: "dns-check-name" }, row, el("div", { className: "dns-records" },
			dnsRecord("A", name, address || "this server's address"),
			...(answer?.host && !expect ? [dnsRecord("CNAME", name, answer.host)] : [])));
	});
}

// A name as typed, as DNS asks for it: no scheme or path, and a name with æ, ø or å in its punycode form
const dnsName = (text) => text.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\/.*$/, "");
const dnsAscii = (name) => { try { return new URL("http://" + name).hostname; } catch { return name; } };

// aliases: a textarea with more names (one per line), each checked as the binding is
function dnsCard(input, { expect = "", name: targetName = "", aliases = null, onState = () => {} } = {}) {
	const status = el("span", { className: "dns-status" });
	const records = el("div", { className: "dns-records" });
	const note = el("p", { className: "dns-note" });
	const card = el("div", { className: "dns-card" },
		el("div", { className: "dns-head" }, el("span", { className: "dns-icon" }, svgIcon("globe")),
			el("strong", { textContent: "DNS for this name" }), status),
		records, note);
	const title = card.querySelector(".dns-head strong");
	// Hidden until a name is checked, so an empty binding never shows it for a moment
	card.hidden = true;
	const record = dnsRecord;
	let timer = null;
	let asked = "";
	const names = () => [...new Set([input.value, ...(aliases ? aliases.value.split(/[\s,]+/) : [])].map(dnsName).filter((name) => name && name !== "*"))];
	// The binding and its aliases: every name gets a row, and the card says how many point here
	const showAll = async (list) => {
		card.hidden = false;
		title.textContent = "DNS for these names";
		card.classList.remove("here");
		card.classList.add("checking", "several");
		status.className = "dns-status checking";
		status.textContent = "Checking…";
		status.title = "";
		asked = list.join(" ");
		const query = (name) => name.includes(".")
			? api(`/api/dns?name=${encodeURIComponent(dnsAscii(name))}${expect ? "&expect=" + encodeURIComponent(expect) : ""}`).catch(() => null)
			: Promise.resolve({ local: true });
		const [answers, server] = await Promise.all([Promise.all(list.map(query)), expect ? null : loadServerAddress().catch(() => null)]);
		if (asked !== list.join(" ")) return;
		const address = expect || server?.address || "";
		const publicNames = answers.filter((answer) => !answer?.local);
		const here = publicNames.filter((answer) => answer?.here).length;
		const ready = here === publicNames.length;
		card.classList.remove("checking");
		card.classList.toggle("here", ready && here > 0);
		records.replaceChildren(el("div", { className: "dns-check-rows" }, ...dnsNameRows(list, answers, address, expect)));
		if (!publicNames.length) { status.className = "dns-status muted"; status.textContent = "Local names: no public DNS"; }
		else if (ready) { status.className = "dns-status ok"; status.textContent = "All names point here"; }
		else { status.className = "dns-status warn"; status.textContent = `${here} of ${publicNames.length} point here`; }
		note.textContent = !expect && server?.behind_nat
			? `The public address could not be found, only an internal one (${address}). Set the public address under Preferences, so the records point to where the internet reaches it.`
			: ready ? "" : "Make the missing records where each domain's DNS is kept. An HTTPS certificate covers every name, so each must point here.";
		note.classList.toggle("warn", Boolean(!expect && server?.behind_nat));
		onState(!publicNames.length ? "local" : ready ? "here" : "elsewhere");
	};
	const show = async () => {
		const list = names();
		if (list.length > 1) return showAll(list);
		title.textContent = "DNS for this name";
		card.classList.remove("several");
		const name = list[0] || "";
		card.hidden = !name;
		if (!name) { onState("empty"); return; }
		const server = expect ? null : await loadServerAddress().catch(() => null);
		const address = expect || server?.address || "";
		// Only an A record to the public address: the server's own host name is rarely public DNS
		const host = targetName;
		records.replaceChildren(record("A", name, address || "this server's address"),
			...(host && host.includes(".") && host !== name ? [record("CNAME", name, host)] : []));
		note.textContent = !expect && server?.behind_nat
			? `The public address could not be found, only an internal one (${address}). Set the public address under Preferences, so the record points to where the internet reaches it.`
			: "Make one of these where the domain's DNS is kept. An HTTPS certificate is issued only when the name points here.";
		note.classList.toggle("warn", Boolean(!expect && server?.behind_nat));
		if (!name.includes(".")) {
			status.className = "dns-status muted";
			status.textContent = "A local name: no public DNS";
			onState("local");
			return;
		}
		asked = name;
		// The records wait for the answer: a name that points here already needs none
		card.classList.remove("here");
		card.classList.add("checking");
		status.className = "dns-status checking";
		status.textContent = "Checking…";
		status.title = "";
		let answer;
		try { answer = await api(`/api/dns?name=${encodeURIComponent(name)}${expect ? "&expect=" + encodeURIComponent(expect) : ""}`); }
		catch (error) { card.classList.remove("checking"); status.className = "dns-status muted"; status.textContent = "Could not check"; onState("unknown"); return; }
		if (asked !== name) return;
		card.classList.remove("checking");
		card.classList.toggle("here", Boolean(answer.here));
		// A subdomain can follow the server's own name instead, so it moves with the server
		if (answer.host && !targetName) records.append(record("CNAME", name, answer.host));
		if (answer.here) { status.className = "dns-status ok"; status.textContent = answer.proxy ? "Via " + answer.proxy : "Points here"; status.title = answer.proxy ? `Proxied by ${answer.proxy}: its addresses stand in for this server's` : ""; }
		else if (answer.addresses.length) { status.className = "dns-status warn"; status.textContent = `Points to ${answer.addresses[0]}`; status.title = answer.addresses.join(", "); }
		else { status.className = "dns-status muted"; status.textContent = "No record yet"; }
		onState(answer.here ? "here" : answer.addresses.length ? "elsewhere" : "none");
	};
	// Typed, or filled in by the dialog (the binding follows the folder name): checked shortly after it changes
	let seen = null;
	const watch = setInterval(() => {
		if (!document.contains(card)) { clearInterval(watch); return; }
		// A closed dialog hides it, so it is checked again when the dialog opens
		if (!input.offsetParent) { card.hidden = true; seen = null; clearTimeout(timer); return; }
		const typed = input.value + "\n" + (aliases ? aliases.value : "");
		if (typed === seen) return;
		seen = typed;
		clearTimeout(timer);
		if (!names().length) { card.hidden = true; onState("empty"); return; }
		timer = setTimeout(() => run(show), 450);
	}, 250);
	return card;
}

// Before a certificate: whether every name of it points to the server that issues it.
// done(true) only when all do; the names that do not are listed with where they point.
function certificateDnsCard({ names, expect = "" }, done) {
	const status = el("span", { className: "dns-status checking", textContent: "Checking…" });
	const rows = el("div", { className: "dns-check-rows" });
	const note = el("p", { className: "dns-note" });
	const card = el("div", { className: "dns-card checking" },
		el("div", { className: "dns-head" }, el("span", { className: "dns-icon" }, svgIcon("globe")),
			el("strong", { textContent: "DNS for the certificate" }), status),
		rows, note);
	const query = (name) => api(`/api/dns?name=${encodeURIComponent(name)}${expect ? "&expect=" + encodeURIComponent(expect) : ""}`).catch(() => null);
	run(async () => {
		const [answers, server] = await Promise.all([Promise.all(names.map(query)),
			expect ? null : loadServerAddress().catch(() => null)]);
		// A dialog that was closed or replaced meanwhile is not told: OK belongs to the dialog open now
		if (!card.isConnected) return;
		const address = expect || server?.address || "";
		const ready = answers.every((answer) => answer?.here);
		card.classList.remove("checking");
		card.classList.toggle("here", ready);
		status.className = "dns-status " + (ready ? "ok" : "warn");
		status.textContent = ready ? (names.length > 1 ? "All names point here" : "Points here") : "Does not point here";
		rows.replaceChildren(...dnsNameRows(names, answers, address, expect));
		note.textContent = ready ? "" : `A certificate can be issued once every name points to ${expect || "this server"}. Change the DNS records, then open Issue certificate again.`;
		done(ready);
	});
	return card;
}

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
function ask({ title, message = "", value = "", placeholder = "", ok = "OK", danger = false, input = true, match = null, extra = null, checkbox = null, checkboxChecked = false, loginCheckbox = false, dns = null, dnsCheck = null }) {
	const dlg = $("#dlg-prompt");
	const field = $("#prompt-input");
	// dnsCheck: names that must point to the server before OK can be used, as for a certificate
	let dnsReady = !dnsCheck;
	// With an extra field (aliases), its names are checked with the main one
	$("#prompt-dns").replaceChildren(...(dns && input ? [dnsCard(field, { ...dns, aliases: extra ? $("#prompt-extra") : null })]
		: dnsCheck ? [certificateDnsCard(dnsCheck, (ready) => { dnsReady = ready; if (dlg.open) updateConfirmation(); })] : []));
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
	const updateConfirmation = () => { okButton.disabled = (match !== null && field.value !== match) || !dnsReady; };
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
	// A user goes to the part they have: SQL Server without the Web Server, and the other way round
	const wantsSql = location.hash.startsWith("#sql") || (!location.hash && startView === "sql");
	// With neither, the sites view tells them who gives access
	if (!currentUser.admin && wantsSql && !currentUser.sql) {
		history.replaceState(null, "", "#sites");
	} else if (!currentUser.admin && !wantsSql && !currentUser.web && currentUser.sql) {
		history.replaceState(null, "", "#sql");
	}
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
	// What the user may do can have changed since the page opened: it is fetched again with the sites
	try { currentUser = await api("/api/me"); accessSnapshot = JSON.stringify(currentUser); applyAccess(); } catch (error) { /* the page goes on with what it had */ }
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
	copy: "M9 9h11v11H9zM5 15H4V4h11v1",
	users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
	rows: "M4 5h16v4H4zM4 15h16v4H4z",
	lock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
	unlock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 7.5-2",
	terminal: "M4 17l6-6-6-6M12 19h8",
	folder: "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z",
	back: "M19 12H5M12 19l-7-7 7-7",
	upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12",
	chevron: "M9 6l6 6-6 6",
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
// Check for updates covers the C# sites the overview shows now: the search and filter decide which
const updateCheckSites = () => overviewSites().filter(matches).filter(canMultiSelect);

function renderMultiSelection() {
	const multipleProjects = listedSites().filter(canMultiSelect).length > 1;
	if (!multipleProjects) {
		multiSelecting = false;
		multiSites.clear();
	}
	$("#btn-check-updates").hidden = !multipleProjects || updateCheckSites().length === 0;
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
	// Check for updates says how many sites it covers when a search or filter narrows them
	const checkable = updateCheckSites().length;
	const narrowed = checkable < listedSites().filter(canMultiSelect).length;
	$("#btn-check-updates").title = narrowed ? `Check the ${checkable} C# ${checkable === 1 ? "site" : "sites"} shown` : "Check all C# sites";
	for (const name of multiSites) {
		if (!listedSites().some((site) => site.name === name && canMultiSelect(site))) multiSites.delete(name);
	}
	renderMultiSelection();
	const box = $("#sites");
	applySiteLayout();
	box.replaceChildren();
	const visible = overviewSites().filter(matches);
	$("#no-sites").hidden = visible.length > 0;
	$("#btn-new-site").hidden = visible.length === 0 || !currentUser.create_sites;
	// Grid or list is a choice only when there are sites to show
	$("#site-layout-toggle").hidden = visible.length === 0;
	// The empty page's own New site, too: only for those who may create sites
	$("#btn-empty-new-site").hidden = !currentUser.create_sites;
	const firstSite = siteCount === 0;
	$("#empty-sites-title").textContent = firstSite ? "Your first site starts here" : "Make room for your next idea";
	$("#empty-sites-description").textContent = firstSite
		? "Create a home for your next idea. Set up your first site in just a few steps."
		: "Try a different search or filter, or bring something new to life by creating a site.";
	// A user without sites, and who cannot make one, is told who gives access
	if (firstSite && !currentUser.create_sites) {
		$("#empty-sites-title").textContent = "No sites for you yet";
		$("#empty-sites-description").textContent = "An administrator gives you access to the sites you work on.";
	}
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
	// The list has room for each environment's state, as its card on the site page shows it
	if (siteLayout === "list") {
		const known = env.unit || (env.mode === "windows" && env.state) || (env.mode === "remote" && env.state && env.state !== "unknown");
		const state = known ? env.state || "unknown" : "";
		const words = { active: "Active", inactive: "Stopped", failed: "Failed", activating: "Starting", deactivating: "Stopping", unknown: "Unknown" };
		row.append(el("span", { className: "env-state", title: env.state_error || "" },
			el("span", { className: "dot " + state }),
			el("span", { className: "badge " + (state ? "state-" + state : ""), textContent: state ? words[state] || state : env.mode === "remote" ? "Remote" : "Static" })));
	}
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
	// The list shows every environment: a row has the room, and there is nothing to unfold
	const expanded = expandedCards.has(site.name) || siteLayout === "list";
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
	} else if (expanded && siteLayout !== "list" && allEnvironments.length > maxCardEnvironments) {
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
		targetsActions.append(el("button", { textContent: "Publish environments", className: "desktop-only", title: "Publish to every environment, not the main site", onclick: () => run(async () => {
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
			targetsActions.append(el("button", { textContent: "Sync data…", className: "desktop-only", title: "Merge content and media from the main site into others", onclick: () => run(() => openSyncDialog(site, mainEnv, true)) }));
		}
		if (syncDestinations(site, mainEnv).length) {
			targetsActions.append(el("button", { textContent: "Sync settings…", className: "desktop-only", title: "Sync settings from the main site", onclick: () => run(() => openSyncDialog(site, mainEnv)) }));
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
		if (envLayout === "list") envListRow(panel, site.name + "/" + (env.target || "prod"), site, env);
		targets.append(panel);
	}
	const canAddEnvironment = site.has_project && !site.admin && (site.kind === "csharp" || site.kind === "umbraco") && canAnywhere(site, "create_env");
	const noEnvironments = !targets.childElementCount;
	if (canAddEnvironment && !noEnvironments) {
		targetsAdd.append(el("button", { textContent: "Add environment", className: "desktop-only", onclick: () => run(() => openCreateProdDialog(site)) }));
	}
	$("#btn-empty-add-environment").onclick = () => run(() => openCreateProdDialog(site));
	$("#site-targets-head").hidden = noEnvironments;
	$("#site-targets-count").textContent = String(targets.childElementCount);
	applyEnvLayout();
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
		packageActions.append(el("button", { textContent: "Add", className: "small package-add desktop-only", title: "Add package", onclick: () => run(() => openPackageDialog(site, "add")) }),
			el("button", { textContent: "Remove", className: "small desktop-only", title: "Remove package", onclick: () => run(() => openPackageDialog(site, "remove")) }));
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
	// Publish all and the packages are the main site's; the environments' buttons follow their own panel
	for (const [part, envKey] of [[head, "main"], [envs, null], [targets, null], [targetsActions, null], [targetsAdd, null],
		[$("#site-danger"), null], [packagesPanel, "main"]]) pruneButtons(part, site, envKey);
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
			// A certificate needs the binding and every alias to point to the server that issues it
			const certificateNames = action === "issue_certificate" ? [env.binding, ...(env.aliases || [])].filter((name) => name && name.includes(".")) : [];
			const dnsCheck = certificateNames.length ? { names: certificateNames, expect: env.mode === "remote" ? env.host || "" : "" } : null;
			if (confirmText && !(await ask({ title: `${label}?`, message: `${confirmText} (${bindingLabel(env) || siteLabel(site)})`, input: false, ok: label, dnsCheck }))) return;
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
	// Copy site copies an environment, and migrates it with Remove it here afterwards where that can be done. Only administrators.
	if (currentUser.admin && isTarget(env) && (env.mode === "local" && env.exists !== false || env.mode === "windows" || env.mode === "remote" && !missing)) addManage("Copy site", () => openCreateProdDialog(site, env, true));
	// An environment in a folder of its own can become a separate site again
	if (currentUser.admin && isTarget(env) && env.separate) addManage("Move out…", () => releaseEnvironment(site, env));
	// The main site: to a new folder here when it has no environments, and with them to another server (administrators),
	// where it can also be migrated
	const copyHere = !hasProd(site);
	const copyToServer = currentUser.admin && Boolean(env.unit || env.apache);
	if (!isTarget(env) && !site.admin && !site.managed_by && (copyHere || copyToServer)) addManage("Copy site", () => copySite(site, env));
	// An environment on Windows is only removed here: its IIS site and files stay on the server
	if (hasProd(site) && !site.admin && env.mode === "windows") addManage("Remove environment", () => deleteSite(site, "prod", env), "push-right");
	else if (hasProd(site) && !site.admin) addManage("Delete site", () => deleteSite(site, isTarget(env) ? "prod" : "test", env), "danger");
	if (!hasProd(site) && !site.admin) addManage("Delete site", () => deleteSite(site, "all"), "danger");

	const panel = el("section", { className: "env " + (isTarget(env) ? "prod" : "") }, heading, meta);
	panel.dataset.env = envKeyOf(env);
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
	// A phone keeps what is used on the go: publish, start or stop, restart, logs and delete.
	// The rest is on the desktop; rows left without buttons are hidden by the stylesheet.
	for (const node of panel.querySelectorAll(".actions > *")) {
		if (!/^(Publish|Start|Stop|Restart|Logs|Delete site|Remove environment)$/.test(node.textContent.trim())) node.classList.add("desktop-only");
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

// The environments as cards (the default) or as a list: a row per environment with its most used buttons,
// which unfolds to the whole card. The choice is remembered in this browser, the unfolded rows while the page is open.
let envLayout = "grid";
try { if (localStorage.getItem("vns-env-layout") === "list") envLayout = "list"; } catch (error) {}
const unfoldedEnvs = new Set();
const envRowButtons = ["Publish", "Clear logs", "Sync data…", "Sync settings…"];
function envListRow(panel, key, site, env) {
	const head = panel.querySelector(".env-head");
	const buttons = [...panel.querySelectorAll("button")];
	// The environment's addresses, as on the overview's cards: the site, and Umbraco's backoffice
	const address = environmentAddress(env);
	const links = el("div", { className: "env-row-links" });
	if (address.url && !isMissingEnv(env)) {
		const frontend = openLink(bindingLabel(env) || address.hostname, address.url, address.title, true);
		frontend.classList.add("frontend");
		links.append(frontend);
		if (site.kind === "umbraco") {
			const backend = openLink("Backend", address.url + "umbraco", "Backoffice: " + address.title, true);
			backend.classList.add("backend");
			links.append(backend);
		}
	}
	const shortcuts = el("div", { className: "env-row-actions" });
	// Each one clicks the card's own button, so it asks and does exactly the same
	for (const label of envRowButtons) {
		const original = buttons.find((button) => button.textContent.trim() === label);
		if (original) shortcuts.append(el("button", { type: "button", className: "small", textContent: label, title: original.title,
			onclick: (event) => { event.stopPropagation(); original.click(); } }));
	}
	const toggle = el("button", { type: "button", className: "env-row-toggle", title: "All buttons and details", ariaLabel: "Show all" }, svgIcon("chevron"));
	const unfold = (open) => {
		panel.classList.toggle("unfolded", open);
		toggle.setAttribute("aria-expanded", String(open));
		if (open) unfoldedEnvs.add(key); else unfoldedEnvs.delete(key);
	};
	toggle.onclick = (event) => { event.stopPropagation(); unfold(!panel.classList.contains("unfolded")); };
	// The whole row unfolds it, except its links and buttons
	head.onclick = (event) => { if (!event.target.closest("a, button, input, label")) unfold(!panel.classList.contains("unfolded")); };
	// The head's parts in columns of the same width in every row, so they line up down the list
	const parts = [...head.children];
	const take = (test) => parts.filter((item) => test(item) && !item.dataset.placed).map((item) => { item.dataset.placed = "1"; return item; });
	const column = (className, items) => el("div", { className: "env-col " + className }, ...items);
	const title = take((item) => item.tagName === "H3");
	if (title[0]) title[0].title = title[0].textContent;
	const status = take((item) => item.classList.contains("site-activity") || item.classList.contains("dot") || /\bstate-/.test(item.className) || /badge (|static|remote|windows)$/.test(item.className));
	const kind = take((item) => /\benv-(?!missing)/.test(item.className));
	const icons = take((item) => item.classList.contains("https-lock") || item.classList.contains("remote-icon"));
	const note = take((item) => item.classList.contains("publish-status") || item.classList.contains("env-missing"));
	head.replaceChildren(column("env-col-name", title), column("env-col-status", status), column("env-col-kind", kind),
		column("env-col-icons", icons), column("env-col-note", [...note, ...parts.filter((item) => !item.dataset.placed)]),
		el("div", { className: "env-col env-col-actions" }, ...(links.childElementCount ? [links] : []), shortcuts), toggle);
	panel.classList.add("env-listed");
	unfold(unfoldedEnvs.has(key));
}
function applyEnvLayout() {
	$("#env-targets").classList.toggle("list", envLayout === "list");
	$("#env-layout-grid").setAttribute("aria-pressed", String(envLayout === "grid"));
	$("#env-layout-list").setAttribute("aria-pressed", String(envLayout === "list"));
}
$("#env-layout-grid").append(svgIcon("grid"));
$("#env-layout-list").append(svgIcon("rows"));
for (const layout of ["grid", "list"]) {
	$("#env-layout-" + layout).onclick = () => {
		envLayout = layout;
		try { localStorage.setItem("vns-env-layout", layout); } catch (error) {}
		const name = currentView === "site" ? decodeURIComponent(location.hash.slice(6)) : "";
		if (name) renderSite(name);
	};
}
applyEnvLayout();

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
		// DNS points at the server the site is on: a remote environment's own host
		dns: env?.mode === "remote" ? { expect: env.host || "" } : env?.mode === "windows" ? { expect: env.host || "" } : {},
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

async function copySite(site, main) {
	const dialog = $("#dlg-copy-site");
	const form = $("#form-copy-site");
	form.reset();
	$("#copy-site-icon").replaceChildren(svgIcon("file"));
	const field = (name) => form.elements.namedItem(name);
	// Only what can be done, and what the user may do: a site with environments is only copied to another server,
	// which takes them along, and only administrators copy or migrate to another server
	const here = !hasProd(site);
	const server = currentUser.admin && Boolean(main && (main.unit || main.apache));
	$("#copy-here-card").hidden = !here;
	field("destination")[0].disabled = !here;
	$("#copy-server-card").hidden = !server;
	field("destination")[1].disabled = !server;
	$("#copy-destination").hidden = !(here && server);
	field("destination").value = here ? "here" : "server";
	const hasSqlConnection = site.environments.some((env) =>
		!isTarget(env) && ["MS SQL", "SQL Express", "Azure SQL"].includes(env.database));
	field("change_connection").closest("label").hidden = !hasSqlConnection;
	field("change_connection").disabled = !hasSqlConnection;
	const sqlFields = $("#copy-sql-fields");
	let databaseEdited = false;
	let bindingEdited = false;
	let usernameEdited = false;
	const suggestUsername = () => {
		if (!usernameEdited) field("username").value = field("database").value;
	};
	const showCopyPath = () => {
		$("#copy-site-path").textContent = `Copied to /var/www/${field("target").value.trim() || "…"}`;
	};
	// A site with a project gets a project of its own
	const hasProject = site.has_project;
	$("#copy-project-row").hidden = !hasProject;
	let projectEdited = false;

	// What goes to another server: the site and its environments here. The others stay where they are.
	const items = sortedEnvironments(site.environments).map((env) => {
		const goes = !isTarget(env) || env.mode === "local";
		const where = goes ? "Goes" : env.mode === "windows" ? "Stays on Windows" : `Stays on ${env.host || "its server"}`;
		return el("li", { className: goes ? "goes" : "stays" },
			el("span", { className: "move-site-name", textContent: bindingLabel(env) || env.folder || siteLabel(site) }),
			el("span", { className: `sync-env-name env-${environmentLabel(site, env).toLowerCase()}`, textContent: environmentLabel(site, env) }),
			el("span", { className: "move-site-where", textContent: where }));
	});
	$("#copy-server-list").replaceChildren(...items);
	const kept = site.environments.some((env) => isTarget(env) && env.mode !== "local");

	const update = () => {
		const toServer = field("destination").value === "server";
		const removing = toServer && field("remove_here").checked;
		$("#copy-here").hidden = toServer;
		$("#copy-here").disabled = toServer;
		$("#copy-server").hidden = !toServer;
		$("#copy-server").disabled = !toServer;
		field("project").required = hasProject && !toServer;
		field("project").disabled = !hasProject || toServer;
		const newConnection = hasSqlConnection && field("change_connection").checked;
		sqlFields.hidden = !newConnection;
		sqlFields.disabled = !newConnection;
		$("#copy-delete-row").hidden = !removing;
		field("delete_local").disabled = !removing;
		if (!removing) field("delete_local").checked = false;
		$("#copy-stop-row").hidden = removing;
		$("#copy-site-title").textContent = removing ? "Migrate site" : "Copy site";
		$("#copy-site-description").textContent = !toServer
			? `Copy ${siteLabel(site)} to a new folder here, with its own service and Apache config.`
			: removing ? `Move ${siteLabel(site)} to another server. It no longer runs here.` : `Copy ${siteLabel(site)} to another server. It stays here as it is.`;
		$("#copy-site-submit").textContent = removing ? "Migrate site" : "Copy site";
		for (const item of $("#copy-server-list").querySelectorAll(".goes .move-site-where")) item.textContent = removing ? "Moves" : "Copied";
		$("#copy-server-hint").textContent = "The whole folders go, including builds, media, logs and SQLite databases, so the sites run the same version and nothing is built. A database in SQL Server stays where it is: connections to this server's own SQL Server are pointed at its address, which must accept connections from the new server."
			+ (kept ? ` Environments on other servers are not touched, and are published to${removing ? " from the new server" : " from both servers"}.` : "")
			+ " The new server needs the same VNS version and CPU architecture, and none of the sites. If a step fails, the sites here run as before.";
	};
	for (const input of field("destination")) input.onchange = update;
	field("remove_here").onchange = update;
	field("change_connection").onchange = update;
	update();
	showCopyPath();

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
	$("#copy-site-cancel").onclick = () => dialog.close();
	dialog.onclose = () => form.reset();
	form.onsubmit = (event) => {
		event.preventDefault();
		run(async () => {
			const submit = form.querySelector('[type="submit"]');
			submit.disabled = true;
			try {
				let payload;
				if (field("destination").value === "server") {
					const removing = field("remove_here").checked;
					payload = { kind: "move_site", site: site.name, host: field("host").value.trim(), ssh_port: field("ssh_port").value,
						user: field("user").value.trim(), password: field("ssh_password").value, copy: !removing,
						delete_local: removing && field("delete_local").checked ? "y" : "n", stop_source: !removing && field("stop_source").checked };
				} else {
					payload = { kind: "copy_site", site: site.name, target: field("target").value.trim(), binding: field("binding").value.trim() };
					if (hasProject) payload.project = field("project").value.trim();
					if (hasSqlConnection && field("change_connection").checked) {
						payload.connection = { server: field("server").value.trim(), database: field("database").value.trim(),
							username: field("username").value.trim(), password: field("password").value };
					}
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

// The clear button empties the search and keeps the cursor in it, as the browser's own did
$("#search-clear").onclick = () => {
	const field = $("#search");
	field.value = "";
	field.dispatchEvent(new Event("input"));
	field.focus();
};
$("#search").oninput = (event) => {
	query = event.target.value;
	if (currentView !== "sites") go("sites");
	renderSites();
};
$("#btn-server-shell").onclick = () => run(() => openTask({ kind: "shell" }));
$("#btn-browse").onclick = () => run(() => openFiles("", "", true));
$("#btn-check-updates").onclick = () => run(async () => {
	// It asks NuGet about every project on the server, which takes a while: only on purpose
	const sites = updateCheckSites().map((site) => site.name);
	if (!sites.length) throw new Error("No sites with a C# project are shown. Change the search or filter.");
	const all = sites.length === listedSites().filter(canMultiSelect).length;
	const which = all ? `all ${sites.length} projects on this server` : `the ${sites.length} ${sites.length === 1 ? "project" : "projects"} shown`;
	if (!await ask({ title: "Check for updates?", message: `Check NuGet for package updates in ${which}. It can take a few minutes. Nothing is installed.`, input: false, ok: "Check for updates" })) return;
	$("#btn-check-updates").disabled = true;
	try {
		updateCheckCurrent.clear();
		updateCheckFeedback = true;
		monitorUpdateCheck(await api("/api/package-updates", { json: { sites } }));
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
	const name = $("#menu-user").textContent;
	profileForm.username.value = name;
	$("#profile-avatar").textContent = name.slice(0, 1).toUpperCase();
	$("#profile-name").textContent = name;
	$("#profile-role").textContent = currentUser.admin ? "Administrator" : "User";
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
	// The templates are for those who may create sites; the dialog loads them when it opens
	if (csharp && currentUser.create_sites) run(loadDotnetTemplates);
}
siteForm.addEventListener("change", (event) => { if (event.target.name === "type") syncSiteForm(); });
// Choosing a kind with the mouse moves on to the folder name. Arrow keys between the kinds keep the focus there.
siteForm.addEventListener("click", (event) => {
	if (event.detail > 0 && event.target.closest(".type-card")) setTimeout(() => siteForm.site.focus());
});
siteForm.elements.template.oninput = syncTemplateVersion;
siteForm.site.oninput = () => { if (!siteForm.binding.dataset.edited) siteForm.binding.value = siteForm.site.value; };
siteForm.binding.oninput = () => { siteForm.binding.dataset.edited = "1"; };
// HTTPS is a choice only when the binding points here: the certificate is issued as the site is created.
// An empty binding is the site for any name, where the server decides. The choice comes back when DNS is right.
const httpsOption = siteForm.certificate.querySelector('option[value="y"]');
const httpsLabel = httpsOption.textContent;
let httpsChosen = true;
function syncHttps(state) {
	if (state === "checking") return;
	const allowed = state === "here" || state === "empty";
	httpsOption.disabled = !allowed;
	httpsOption.textContent = allowed ? httpsLabel : "HTTPS - possible once DNS points here";
	if (!allowed && siteForm.certificate.value === "y") siteForm.certificate.value = "n";
	else if (allowed && httpsChosen) siteForm.certificate.value = "y";
}
siteForm.certificate.addEventListener("change", () => { httpsChosen = siteForm.certificate.value === "y"; });
$("#site-dns").replaceChildren(dnsCard(siteForm.binding, { onState: syncHttps }));
$("#setup-site-dns").replaceChildren(dnsCard($("#form-setup-site").binding));
// The binding's message is about the certificate choice as well: any change clears it, or the browser keeps
// refusing the form before it is checked again
for (const type of ["input", "change"]) siteForm.addEventListener(type, () => siteForm.binding.setCustomValidity(""));
$("#btn-new-site").onclick = () => { siteForm.reset(); siteForm.elements.template.setCustomValidity(""); siteForm.binding.setCustomValidity(""); delete siteForm.binding.dataset.edited; syncSiteForm(); dialog.showModal(); siteForm.site.focus(); };
$("#btn-new-site").addEventListener("click", () => { httpsChosen = true; syncHttps("empty"); });
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
	const same = target === "same";
	const sameServerMove = moving && !copying && local && prodForm.dataset.moveMode === "local";
	const noSettings = sameServerMove;
	$("#prod-folder-row").hidden = moving && local || noSettings;
	$("#prod-folder-label").textContent = windows ? "Folder name" : "Folder / service name";
	// On Windows the IIS site's folder is the share and the folder name, shown in full
	const share = prodForm.smb_share.value.trim().replace(/\\/g, "/").replace(/\/+$/, "");
	const folderName = prodForm.folder.value.trim();
	$("#prod-windows-path").textContent = `IIS site folder: ${share || "//server/share"}${share && share.split("/").pop().toLowerCase() === folderName.toLowerCase() ? "" : "/" + (folderName || "…")}`;
	$("#prod-binding-row").hidden = noSettings && !sameServerMove;
	$("#prod-environment-row").hidden = noSettings;
	// To another server it starts with HTTP, as DNS may not point there yet. Issue certificate turns HTTPS on.
	const otherServer = moving && !same && !(local && prodForm.dataset.moveMode === "local");
	if (otherServer && !prodForm.dataset.httpsChoice) {
		prodForm.dataset.httpsChoice = prodForm.elements.https.value;
		prodForm.elements.https.value = "n";
	} else if (!otherServer && prodForm.dataset.httpsChoice) {
		prodForm.elements.https.value = prodForm.dataset.httpsChoice;
		delete prodForm.dataset.httpsChoice;
	}
	$("#prod-protocol-row").hidden = noSettings || otherServer;
	$("#prod-protocol-hint").hidden = noSettings;
	if (otherServer) $("#prod-protocol-hint").textContent = "It is set up with HTTP on the new server, as DNS may not point there yet. Use Issue certificate on the environment when it does.";
	prodForm.folder.required = !(moving && local) && !noSettings;
	prodForm.binding.required = true;
	const fromWindows = prodForm.dataset.moveMode === "windows";
	const hasSql = fromWindows || prodForm.dataset.moveSql === "1";
	const copyOptions = copying;
	$("#prod-copy-options").hidden = !copyOptions;
	$("#prod-stop-row").hidden = prodForm.dataset.moveDatabase !== "SQLite";
	$("#prod-connection-row").hidden = !hasSql;
	const newConnection = copyOptions && hasSql && prodForm.elements.change_connection.checked;
	$("#prod-sql-fields").hidden = !newConnection;
	$("#prod-sql-fields").disabled = !newConnection;
	// Copying is where it starts; where it can be done, Remove it here afterwards makes it a migration
	$("#prod-remove-options").hidden = !moving || !prodForm.dataset.canMigrate;
	prodForm.elements.remove_here.disabled = !prodForm.dataset.canMigrate;
	$("#prod-remove-sub").textContent = same || local && prodForm.dataset.moveMode === "local"
		? "Move it to the new folder: it keeps its name and binding, and the service and Apache config follow it"
		: "Migrate: it keeps its name and binding, and is removed where it is once it runs at the destination";
	$("#prod-delete-row").hidden = copying || sameServerMove;
	$("#prod-move-hint").textContent = windows
		? "Settings and content are copied, and the project is built for Windows. A connection to the source's own SQL Server is changed to its address, which must accept connections from the new server. With a Windows target, the IIS site and its folder must already exist."
		: fromWindows
			? "Settings and content are copied from Windows while its site runs, and the project is built for the destination. A connection to the Windows server's own SQL Server is changed to its address."
			: `${copying ? "The whole folder is copied" : "The site is stopped while the whole folder is copied"}, including its build, media, logs and a SQLite database, so it runs the same version. A connection to the source's own SQL Server is changed to its address, which must accept connections from the new server.`;
	const destinationServer = same ? prodForm.dataset.moveHost : target === "remote" ? prodForm.host.value.trim() || "…" : "";
	const destinationPath = windows ? prodForm.smb_share.value.trim() || "…"
		: local ? prodForm.local_path.value.trim() || "…"
			: `${destinationServer}:/var/www/${prodForm.folder.value.trim() || "…"}`;
	$("#prod-dest-path").hidden = !moving;
	$("#prod-dest-path").textContent = `${copying ? "Copied" : "Migrated"} to ${destinationPath}`;
	$("#prod-move-options").hidden = !moving || sameServerMove;
	prodForm.elements.delete_local.disabled = copying || sameServerMove;
	if (prodForm.elements.delete_local.disabled) prodForm.elements.delete_local.checked = false;
	$("#prod-submit").textContent = !moving ? "Add environment" : copying ? "Copy" : "Migrate";
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
	delete prodForm.dataset.httpsChoice;
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
	$("#prod-title").textContent = "Add environment";
	$("#prod-description").textContent = "Configure where this site is published to, then publish and sync content automatically when available.";
	// An environment on Windows can only be copied: its IIS site cannot be stopped or removed from here
	prodForm.dataset.canMigrate = moveEnv && moveEnv.mode !== "windows" ? "1" : "";
	prodForm.elements.remove_here.checked = Boolean(moveEnv && !copying && prodForm.dataset.canMigrate);
	// A remote environment can also go to another folder on its own server
	const targets = $("#prod-target-row");
	for (const value of ["same"]) targets.querySelector(`input[value=${value}]`)?.closest("label").remove();
	const targetCard = (value, title, sub) => el("label", { className: "type-card" },
		el("input", { type: "radio", name: "target", value }), el("span", { className: "tc-title", textContent: title }), el("span", { className: "tc-sub", textContent: sub }));
	if (moveEnv?.mode === "remote") targets.prepend(targetCard("same", "Same server", moveEnv.host || "Another folder there"));
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
	prodForm.elements.target.value = moveEnv?.mode === "remote" ? "same" : "local";
	prodForm.elements.https.value = main && main.scheme === "http" ? "n" : "y";
	prodMoveContext = moveEnv ? { site, moveEnv, binding } : null;
	if (moveEnv) {
		applyProdMoveMode();
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
// Copy or migrate: a copy is a new environment with its own name and binding, a migration keeps them unless they are changed
let prodMoveContext = null;
function applyProdMoveMode() {
	if (!prodMoveContext) return;
	const { site, moveEnv, binding } = prodMoveContext;
	const copying = !prodForm.elements.remove_here.checked;
	if (copying) prodForm.dataset.copy = "1";
	else delete prodForm.dataset.copy;
	const label = bindingLabel(moveEnv) || moveEnv.unit || moveEnv.folder;
	$("#prod-title").textContent = `${copying ? "Copy" : "Migrate"} ${environmentLabel(site, moveEnv)}`;
	$("#prod-description").textContent = copying
		? `Copy ${label} to a new folder or server. It stays as it is, and the copy becomes a new environment here.`
		: `Migrate ${label} to another folder or server. Everything goes with it.`;
	const name = copying ? "" : moveEnv.unit || moveEnv.folder || siteLabel(site);
	const movedBinding = copying ? "" : bindingLabel(moveEnv) || binding;
	prodForm.folder.value = name;
	prodForm.binding.value = movedBinding;
	if (copying) delete prodForm.binding.dataset.edited;
	else prodForm.binding.dataset.edited = "1";
	prodForm.local_path.value = `/var/www/${name}`;
	prodForm.dataset.remoteFolder = name;
	prodForm.dataset.remoteBinding = movedBinding;
	prodForm.dataset.localFolder = name;
	prodForm.dataset.localBinding = movedBinding;
}
prodForm.elements.remove_here.onchange = () => {
	applyProdMoveMode();
	syncProdForm();
};
// Move out: the environment becomes a separate site again, in the folder it has
async function releaseEnvironment(site, env) {
	if (!(await ask({ title: "Move out?", input: false, ok: "Move out",
		message: `${bindingLabel(env) || env.folder} becomes a separate site again, in the folder it already has. Its files, service and Apache config are not touched, and it can no longer be published from ${siteLabel(site)} until it is moved back.` }))) return;
	await openTask({ kind: "release_site", site: site.name, target: env.target });
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
	for (const key of ["conn_server", "conn_database", "conn_username", "conn_password", "change_connection", "remove_here"]) delete data[key];
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
$("#targets-search-clear").onclick = () => {
	const field = $("#targets-search");
	field.value = "";
	filterTargets();
	field.focus();
};
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
	// Removing is choosing one of the project's packages; adding names a new one
	const name = packageForm.elements.namedItem("package");
	$("#package-name-row").hidden = !adding;
	$("#package-pick").hidden = adding;
	const submit = $("#package-submit");
	submit.classList.toggle("danger-primary", !adding);
	submit.textContent = adding ? "Add package" : "Remove package";
	submit.disabled = !adding;
	if (!adding) {
		const packages = [...(site.packages || [])].sort((a, b) => a.name.localeCompare(b.name));
		const list = $("#package-list");
		packageForm.dataset.packages = "[]";
		list.setAttribute("role", "group");
		// One or more: a click picks a package or puts it back
		const choose = (item, row) => {
			row.setAttribute("aria-checked", String(row.getAttribute("aria-checked") !== "true"));
			const chosen = [...list.querySelectorAll('.package-pick-row[aria-checked="true"]')].map((other) => other.dataset.package);
			name.value = chosen[0] || "";
			packageForm.dataset.packages = JSON.stringify(chosen);
			submit.textContent = chosen.length > 1 ? `Remove ${chosen.length} packages` : chosen.length ? "Remove " + chosen[0] : "Remove package";
			submit.disabled = !chosen.length;
		};
		list.replaceChildren(...(packages.length ? packages.map((item) => {
			const row = el("button", { type: "button", className: "package-pick-row", role: "radio", ariaChecked: "false" },
				el("span", { className: "package-pick-icon" }, svgIcon("package")),
				el("span", { className: "package-pick-name", textContent: item.name }),
				el("span", { className: "package-pick-version", textContent: item.version || "" }));
			row.dataset.name = item.name.toLowerCase();
			row.dataset.package = item.name;
			row.setAttribute("role", "checkbox");
			row.onclick = () => choose(item, row);
			return row;
		}) : [el("p", { className: "muted", textContent: "The project has no packages." })]));
		$("#package-filter").value = "";
		$("#package-filter").hidden = packages.length < 7;
		$("#package-filter").oninput = () => {
			const words = $("#package-filter").value.trim().toLowerCase();
			for (const row of list.querySelectorAll(".package-pick-row")) row.hidden = Boolean(words) && !row.dataset.name.includes(words);
		};
	}
	packageDialog.showModal();
	(adding ? name : $("#package-filter").hidden ? $("#package-list .package-pick-row") : $("#package-filter"))?.focus();
}
$("#package-cancel").onclick = () => packageDialog.close();
packageForm.onsubmit = (event) => {
	event.preventDefault();
	const data = Object.fromEntries(new FormData(packageForm));
	data.kind = "dotnet_package";
	data.site = packageForm.dataset.site;
	data.action = packageForm.dataset.action;
	if (data.action === "remove") data.packages = JSON.parse(packageForm.dataset.packages || "[]");
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
// ------------------------------------------------------------------ config snippets
// Ready-made rules for an IIS web.config or an Apache site config, put in the right place in the file that is open.
// The file only changes in the editor, so the rules can be read and adjusted before Save.
// They do what the rewrite rules on the Windows servers do, in the same order whichever is added first:
// Ignore, Allowed IPs, AddTrailingSlash, Remove www, Rewrite to a domain, HTTP to HTTPS.
const regexEscape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const xmlEscape = (text) => text.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const snippetDomain = /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
const snippetPath = /^[A-Za-z0-9._~\-/]+$/;
const snippetOrder = ["Ignore", "Allowed IPs", "AddTrailingSlash", "Remove www", "Rewrite to domain", "Rewrite HTTP to HTTPS"];

// What a snippet asks for is read and checked here; a mistake is shown in its window, which stays open
function listOf(value, valid, error) {
	const items = String(value || "").split(/[\s,]+/).map((item) => item.replace(/^\/+|\/+$/g, "")).filter(Boolean);
	if (!items.length || items.some((item) => !valid(item))) throw new Error(error);
	return items;
}
const pathsOf = (value) => listOf(value, (path) => snippetPath.test(path), "Enter paths like members/area or shop.");
const addressesOf = (value, cidr) => listOf(value, (address) => (cidr ? /^[0-9a-fA-F:.]+(\/\d{1,3})?$/ : /^[0-9a-fA-F:.]+$/).test(address) && /[.:]/.test(address),
	cidr ? "Enter IP addresses or ranges, like 192.0.2.10 or 10.0.0.0/24." : "Enter single IP addresses, like 192.0.2.10.");
function domainOf(value) {
	const domain = String(value || "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\/.*$/, "");
	if (!snippetDomain.test(domain)) throw new Error("Enter a domain name like example.dk.");
	return domain;
}
function rewriteOf(values) {
	const clean = (value) => String(value || "").trim().replace(/^\/+/, "");
	const rewrite = { from: clean(values.from), to: clean(values.to) };
	if (![rewrite.from, rewrite.to].every((path) => path && snippetPath.test(path))) throw new Error("Enter both paths, like old/page and new/page.");
	return rewrite;
}
const pathPattern = (paths) => paths.map(regexEscape).join("|");

// The fields a snippet asks for, the same for IIS and Apache
const snippetFields = {
	ignore: { description: "The rules after this one leave these paths alone.", fields: [
		{ name: "paths", label: "Paths to leave alone", value: "umbraco/api, umbraco/surface, umbraco/backoffice", hint: "Separate them with spaces or commas." }] },
	ips: { description: "Only these addresses reach the protected paths. This server itself always does.", fields: [
		{ name: "addresses", label: "Allowed IP addresses", placeholder: "192.0.2.10, 198.51.100.7", hint: "Separate them with spaces or commas." },
		{ name: "paths", label: "Paths to protect", value: "umbraco, app_plugins, config" },
		{ name: "response", label: "Others get", options: [["forbidden", "403 Forbidden"], ["notfound", "404 Not found, as if the path was not there"], ["redirect", "A redirect to another address"], ["abort", "The connection closed, without an answer", "iis"]] },
		{ name: "target", label: "Redirect to", value: "/", showIf: ["response", "redirect"], hint: "A path on the site, like /, or a whole address." }] },
	domain: { description: "Every other name the site answers on is sent to this one.", fields: [
		{ name: "domain", label: "Domain", placeholder: "example.dk" }] },
	rewrite: { description: "Visitors ask for the first path and get what the site has at the second. The address they see does not change.", fields: [
		{ name: "from", label: "From", placeholder: "old/page" },
		{ name: "to", label: "To", placeholder: "new/page" }] },
};

// Where a redirect for Allowed IPs may go: a path on the site, or an http(s) address
function redirectTargetOf(value) {
	const target = String(value || "").trim() || "/";
	if (!/^(\/[^\s"'<>]*|https?:\/\/[^\s"'<>]+)$/.test(target)) throw new Error("Redirect to a path like / or an address like https://example.dk/.");
	return target;
}

// IIS: each rule as web.config writes it
const iisSnippets = {
	ignore: { label: "Ignore", build: (v) => `<rule name="Ignore" stopProcessing="true" enabled="true">
  <match url="^(?:${xmlEscape(pathPattern(pathsOf(v.paths)))})/" />
  <action type="None" />
</rule>` },
	ips: { label: "Allowed IPs", build: (v) => {
		const addresses = addressesOf(v.addresses, false);
		const listed = xmlEscape(addresses.map(regexEscape).join("|"));
		return `<rule name="${xmlEscape(v.name || "Allowed IPs")}" stopProcessing="true" enabled="true">
  <match url="^(?:${xmlEscape(pathPattern(pathsOf(v.paths)))})(?:/|$)" />
  <conditions>
    <add input="{REMOTE_ADDR}" matchType="Pattern" pattern="^(?:::1|127\\.0\\.0\\.1|${listed})$" ignoreCase="true" negate="true" />
    <add input="{HTTP_X_FORWARDED_FOR}" matchType="Pattern" pattern="(?:^|,\\s*)(?:${listed})$" ignoreCase="true" negate="true" />
  </conditions>
  ${{
		forbidden: '<action type="CustomResponse" statusCode="403" statusReason="Forbidden" statusDescription="Access denied" />',
		notfound: '<action type="CustomResponse" statusCode="404" statusReason="Not Found" statusDescription="Not found" />',
		redirect: `<action type="Redirect" url="${xmlEscape(redirectTargetOf(v.target))}" redirectType="Found" />`,
	}[v.response] || '<action type="AbortRequest" />'}
</rule>`;
	} },
	slash: { label: "AddTrailingSlash", build: () => `<rule name="AddTrailingSlash" stopProcessing="true" enabled="true">
  <match url=".*" />
  <conditions logicalGrouping="MatchAll">
    <!-- Not for files, Umbraco's paths, or an address that has a slash already -->
    <add input="{REQUEST_FILENAME}" matchType="IsFile" negate="true" />
    <add input="{REQUEST_URI}" pattern="\\.[a-zA-Z0-9]{2,10}($|\\?)" negate="true" />
    <add input="{REQUEST_URI}" pattern="^/umbraco" negate="true" />
    <add input="{REQUEST_URI}" pattern="^/media" negate="true" />
    <add input="{REQUEST_URI}" pattern="/$" negate="true" />
  </conditions>
  <action type="Redirect" url="{R:0}/" redirectType="Permanent" />
</rule>` },
	www: { label: "Remove www", build: () => `<rule name="Remove www" stopProcessing="true" enabled="true">
  <match url="(.*)" ignoreCase="true" />
  <conditions logicalGrouping="MatchAll">
    <add input="{HTTP_HOST}" pattern="^www\\.(.+)$" />
  </conditions>
  <action type="Redirect" url="https://{C:1}/{R:0}" appendQueryString="true" redirectType="Permanent" />
</rule>` },
	domain: { label: "Rewrite to domain", build: (v) => {
		const domain = domainOf(v.domain);
		return `<rule name="Rewrite to domain" stopProcessing="true" enabled="true">
  <match url="(.*)" />
  <conditions>
    <add input="{HTTP_HOST}" pattern="^${xmlEscape(regexEscape(domain))}$" negate="true" />
  </conditions>
  <action type="Redirect" url="https://${domain}/{R:0}" appendQueryString="true" redirectType="Permanent" />
</rule>`;
	} },
	https: { label: "Rewrite HTTP to HTTPS", build: () => `<rule name="Rewrite HTTP to HTTPS" stopProcessing="true" enabled="true">
  <match url="(.*)" />
  <conditions>
    <add input="{HTTPS}" pattern="^OFF$" />
  </conditions>
  <action type="Redirect" url="https://{HTTP_HOST}{REQUEST_URI}" appendQueryString="false" />
</rule>` },
	rewrite: { label: "Rewrite from → to", build: (v) => {
		const rewrite = rewriteOf(v);
		return `<rule name="Rewrite ${xmlEscape(rewrite.from)}" stopProcessing="true" enabled="true">
  <match url="^${xmlEscape(regexEscape(rewrite.from))}$" />
  <action type="Rewrite" url="${xmlEscape(rewrite.to)}" appendQueryString="true" />
</rule>`;
	} },
	// Not a rewrite rule: a section of its own in <system.webServer>
	compression: { label: "Compression", section: "httpCompression", build: () => `<httpCompression directory="%SystemDrive%\\inetpub\\temp\\IIS Temporary Compressed Files">
  <scheme name="gzip" dll="%Windir%\\system32\\inetsrv\\gzip.dll" />
  <dynamicTypes>
    <add mimeType="text/*" enabled="true" />
    <add mimeType="application/javascript" enabled="true" />
    <add mimeType="application/json" enabled="true" />
  </dynamicTypes>
  <staticTypes>
    <add mimeType="text/*" enabled="true" />
    <add mimeType="application/javascript" enabled="true" />
    <add mimeType="application/json" enabled="true" />
  </staticTypes>
</httpCompression>` },
	cookies: { label: "Add Secure to ALL cookies", outbound: true, build: () => `<rule name="Add Secure to ALL cookies" preCondition="NeedsSecureFlag">
  <match serverVariable="RESPONSE_Set-Cookie" pattern="(.*)" />
  <action type="Rewrite" value="{R:1}; secure" />
</rule>`, preCondition: `<preCondition name="NeedsSecureFlag">
  <add input="{RESPONSE_Set-Cookie}" pattern="secure" negate="true" />
</preCondition>` },
};

// Apache: the same rules for the HTTPS site. HTTP to HTTPS is not one: every site's HTTP part already sends there.
// The first line of each is its name, as a comment, so it is found again.
const apacheSnippets = {
	ignore: { label: "Ignore", build: (v) => `# Ignore
RewriteRule ^/(?:${pathPattern(pathsOf(v.paths))})/ - [L]` },
	ips: { label: "Allowed IPs", build: (v) => {
		const addresses = addressesOf(v.addresses, true);
		// An expression, as mod_rewrite cannot match ranges; anyone else gets 403. The addresses are joined with ||
		// under one !: in Apache's expressions ! covers everything after it, so "! a && ! b" does not mean "neither".
		return `# ${v.name || "Allowed IPs"}
RewriteCond expr "!(${["127.0.0.0/8", "::1", ...addresses].map((address) => `-R '${address}'`).join(" || ")})"
RewriteRule ^/(?:${pathPattern(pathsOf(v.paths))})(?:/|$) ${{
		notfound: "- [R=404,NC,L]",
		redirect: `${redirectTargetOf(v.target)} [R=302,NC,L]`,
	}[v.response] || "- [F,NC,L]"}`;
	} },
	slash: { label: "AddTrailingSlash", build: () => `# AddTrailingSlash
RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$
RewriteCond %{REQUEST_URI} !\\.[a-zA-Z0-9]{2,10}$
RewriteCond %{REQUEST_URI} !^/umbraco [NC]
RewriteCond %{REQUEST_URI} !^/media [NC]
RewriteCond %{REQUEST_URI} !/$
RewriteRule ^(.*)$ $1/ [R=301,L]` },
	www: { label: "Remove www", build: () => `# Remove www
RewriteCond %{HTTP_HOST} ^www\\.(.+)$ [NC]
RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L]` },
	domain: { label: "Rewrite to domain", build: (v) => {
		const domain = domainOf(v.domain);
		return `# Rewrite to domain
RewriteCond %{HTTP_HOST} !^${regexEscape(domain)}$ [NC]
RewriteRule ^ https://${domain}%{REQUEST_URI} [R=301,L]`;
	} },
	// Sent straight to the site's service with P: ProxyPass ... nocanon passes on the address as the visitor wrote it,
	// so a rewrite that only changes the path inside Apache (PT) never reaches the service
	rewrite: { label: "Rewrite from → to", build: (v, config) => {
		const port = /^[ \t]*ProxyPass\s+\/\s+http:\/\/127\.0\.0\.1:(\d+)\//m.exec(config);
		if (!port) throw new Error("This config has no ProxyPass / to the site's service, so a rewrite cannot be sent there.");
		const rewrite = rewriteOf(v);
		return `# Rewrite ${rewrite.from}
RewriteRule ^/${regexEscape(rewrite.from)}$ http://127.0.0.1:${port[1]}/${rewrite.to} [P,L]`;
	} },
	// Ubuntu's Apache compresses HTML, CSS and JavaScript already; this adds JSON, SVG and the rest of text for the site
	compression: { label: "Compression", place: "end", build: () => `# Compression
<IfModule mod_deflate.c>
  AddOutputFilterByType DEFLATE text/html text/plain text/css text/xml text/javascript application/javascript application/json application/xml image/svg+xml
</IfModule>` },
	cookies: { label: "Add Secure to ALL cookies", place: "end", build: () => `# Add Secure to ALL cookies
Header always edit Set-Cookie "(?i)^((?!.*;\\s*secure).*)$" "$1; Secure"` },
};

// The window a snippet opens: all its fields at once. It answers with the snippet's text, or null when cancelled.
function askSnippet(key, snippet, config) {
	const spec = snippetFields[key] || { description: "", fields: [] };
	const dialog = $("#dlg-snippet");
	const form = $("#form-snippet");
	const error = $("#snippet-error");
	$("#snippet-title").textContent = snippet.label;
	$("#snippet-description").textContent = spec.description || "Inserted as it is. Check it in the file before you save.";
	const kind = snippetKind();
	const rows = spec.fields.map((field) => {
		const input = field.options
			? el("select", { name: field.name }, ...field.options.filter((option) => !option[2] || option[2] === kind)
				.map(([value, text]) => el("option", { value, textContent: text })))
			: el("input", { name: field.name, value: field.value || "", placeholder: field.placeholder || "", autocomplete: "off", spellcheck: false });
		const row = el("label", {}, field.label, input, ...(field.hint ? [el("small", { className: "hint", textContent: field.hint })] : []));
		row.dataset.field = field.name;
		return [field, row];
	});
	$("#snippet-fields").replaceChildren(...rows.map(([, row]) => row));
	// A field that belongs to one choice, like the address of a redirect, is only there when it is chosen
	const showFields = () => {
		for (const [field, row] of rows) {
			if (!field.showIf) continue;
			const chosen = form.elements[field.showIf[0]]?.value === field.showIf[1];
			row.hidden = !chosen;
			row.querySelector("input, select").disabled = !chosen;
		}
	};
	form.onchange = showFields;
	showFields();
	error.textContent = "";
	return new Promise((resolve) => {
		let result = null;
		form.onsubmit = (event) => {
			event.preventDefault();
			try {
				const values = Object.fromEntries(new FormData(form));
				result = { text: snippet.build(values, config), values };
				dialog.close();
			} catch (problem) {
				error.textContent = problem.message;
			}
		};
		$("#snippet-cancel").onclick = () => dialog.close();
		dialog.onclose = () => resolve(result);
		dialog.showModal();
		form.querySelector("input")?.focus();
	});
}

// Each Apache snippet keeps what it was made from on its second line, "# vns {...}": a publish reads it there
// and writes the same snippet for the site's environments, in their own form (lib/config_snippets.py)
for (const [key, snippet] of Object.entries(apacheSnippets)) {
	const build = snippet.build;
	snippet.build = (values, config) => {
		const [first, ...rest] = build(values, config).split("\n");
		const kept = Object.fromEntries(Object.entries(values || {}).filter(([name, value]) => name !== "name" && value !== ""));
		return [first, "# vns " + JSON.stringify({ key, values: kept }), ...rest].join("\n");
	};
}

// The indentation of the line that holds position, and a block indented to it
const lineIndent = (text, position) => (text.slice(text.lastIndexOf("\n", position - 1) + 1).match(/^[ \t]*/) || [""])[0];
const indentBlock = (block, indent) => block.split("\n").map((line) => (line ? indent + line : line)).join("\n");
const lineStart = (text, position) => text.lastIndexOf("\n", position - 1) + 1;
const rank = (name) => snippetOrder.findIndex((entry) => name === entry || name.startsWith(entry + " "));

function insertIisRule(text, name, rule, snippet) {
	if (new RegExp(`<rule\\s+name="${regexEscape(xmlEscape(name))}"`).test(text)) throw new Error(`web.config already has the rule "${name}".`);
	const step = "  ";
	if (snippet.outbound) {
		const preCondition = snippet.preCondition;
		let result = text;
		const outbound = /<outboundRules\b[^>]*>/.exec(result);
		if (outbound) {
			const indent = lineIndent(result, outbound.index) + step;
			result = result.slice(0, outbound.index + outbound[0].length) + "\n" + indentBlock(rule, indent) + result.slice(outbound.index + outbound[0].length);
			const conditions = /<preConditions\b[^>]*>/.exec(result.slice(outbound.index));
			if (conditions) {
				const at = outbound.index + conditions.index + conditions[0].length;
				return result.slice(0, at) + "\n" + indentBlock(preCondition, lineIndent(result, at) + step) + result.slice(at);
			}
			const close = result.indexOf("</outboundRules>", outbound.index);
			const start = lineStart(result, close);
			return result.slice(0, start) + indentBlock(`<preConditions>\n${indentBlock(preCondition, step)}\n</preConditions>`, indent) + "\n" + result.slice(start);
		}
		const block = `<outboundRules>\n${indentBlock(rule, step)}\n${step}<preConditions>\n${indentBlock(preCondition, step + step)}\n${step}</preConditions>\n</outboundRules>`;
		const rulesEnd = /<\/rules>/.exec(result);
		if (rulesEnd) {
			const at = rulesEnd.index + rulesEnd[0].length;
			return result.slice(0, at) + "\n" + indentBlock(block, lineIndent(result, rulesEnd.index)) + result.slice(at);
		}
		return insertRewriteBlock(result, `<rewrite>\n${indentBlock(block, step)}\n</rewrite>`);
	}
	const rules = /<rewrite\b[^>]*>\s*<rules\b[^>]*>/.exec(text);
	if (!rules) return insertRewriteBlock(text, `<rewrite>\n${step}<rules>\n${indentBlock(rule, step + step)}\n${step}</rules>\n</rewrite>`);
	const open = rules.index + rules[0].length;
	const close = text.indexOf("</rules>", open);
	const existing = [...text.slice(open, close).matchAll(/<rule\s+name="([^"]*)"/g)].map((match) => ({ name: match[1], at: open + match.index }));
	const mine = rank(name);
	// Before the first rule of the list that comes after this one, else after the last that comes before it
	const later = mine < 0 ? null : existing.find((item) => rank(item.name) > mine);
	if (later) {
		const start = lineStart(text, later.at);
		return text.slice(0, start) + indentBlock(rule, lineIndent(text, later.at)) + "\n" + text.slice(start);
	}
	// Rules of the same kind (Allowed IPs 2 after Allowed IPs) come before a new one too
	const earlier = mine < 0 ? [] : existing.filter((item) => rank(item.name) >= 0 && rank(item.name) <= mine);
	if (earlier.length) {
		const last = earlier[earlier.length - 1];
		const after = text.indexOf("</rule>", last.at) + "</rule>".length;
		return text.slice(0, after) + "\n" + indentBlock(rule, lineIndent(text, last.at)) + text.slice(after);
	}
	const indent = existing.length ? lineIndent(text, existing[0].at) : lineIndent(text, rules.index) + step + step;
	// Ignore and Allowed IPs go first, the redirects last
	if (mine >= 0 && mine <= 1) return text.slice(0, open) + "\n" + indentBlock(rule, indent) + text.slice(open);
	const start = lineStart(text, close);
	return text.slice(0, start) + indentBlock(rule, indent) + "\n" + text.slice(start);
}

// A section of <system.webServer> of its own, like <httpCompression>, at its end; only one of each
function insertIisSection(text, tag, block) {
	if (new RegExp(`<${tag}\\b`).test(text)) throw new Error(`web.config already has <${tag}>.`);
	const close = text.indexOf("</system.webServer>");
	if (close < 0) throw new Error("This web.config has no <system.webServer> section to put it in.");
	const start = lineStart(text, close);
	return text.slice(0, start) + indentBlock(block, lineIndent(text, close) + "  ") + "\n" + text.slice(start);
}

// A new <rewrite> section, at the end of the site's <system.webServer>
function insertRewriteBlock(text, block) {
	const rewrite = /<rewrite\b[^>]*>/.exec(text);
	if (rewrite) {
		const at = rewrite.index + rewrite[0].length;
		const inner = block.replace(/^<rewrite>\n/, "").replace(/\n<\/rewrite>$/, "");
		return text.slice(0, at) + "\n" + indentBlock(inner, lineIndent(text, rewrite.index)) + text.slice(at);
	}
	const close = text.indexOf("</system.webServer>");
	if (close < 0) throw new Error("This web.config has no <system.webServer> section to put the rule in.");
	const indent = lineIndent(text, close);
	const start = lineStart(text, close);
	return text.slice(0, start) + indentBlock(block, indent + "  ") + "\n" + text.slice(start);
}

function insertApacheSnippet(text, name, snippet, place) {
	const marker = "# " + name;
	if (text.split("\n").some((line) => line.trim() === marker)) throw new Error(`The config already has "${name}".`);
	// The HTTPS site when there is one: that is where visitors end up
	const hosts = [...text.matchAll(/<VirtualHost\b[^>]*>/g)];
	if (!hosts.length) throw new Error("This file has no <VirtualHost> to put the rule in.");
	const host = hosts.find((match) => /:443>/.test(match[0])) || hosts[0];
	const start = host.index + host[0].length;
	const end = text.indexOf("</VirtualHost>", start);
	const indent = lineIndent(text, text.indexOf("\n", start) + 1) || "  ";
	const block = indentBlock(marker + "\n" + snippet, indent);
	const body = text.slice(start, end);
	if (place === "end") {
		const before = text.slice(0, end).replace(/[ \t]*$/, "");
		return before + "\n" + block + "\n" + lineIndent(text, end) + text.slice(end);
	}
	const proxy = /^[ \t]*(ProxyPass|ProxyPreserveHost)\b/m.exec(body);
	// The rules of the list keep their order: before the first that comes after this one, else after the last before it
	const markers = [...body.matchAll(/^[ \t]*# (.+)$/gm)].map((match) => ({ name: match[1].trim(), at: start + match.index })).filter((item) => rank(item.name) >= 0);
	const mine = rank(name);
	if (mine < 0) {
		const at = proxy ? start + proxy.index : end;
		return text.slice(0, at) + block + "\n\n" + text.slice(at);
	}
	const later = markers.find((item) => rank(item.name) > mine);
	if (later) return text.slice(0, later.at) + block + "\n\n" + text.slice(later.at);
	const earlier = markers.filter((item) => rank(item.name) <= mine);
	if (earlier.length) {
		const last = earlier[earlier.length - 1];
		const blank = text.indexOf("\n\n", last.at);
		const at = blank < 0 || blank > end ? end : blank + 1;
		return text.slice(0, at) + "\n" + block + "\n" + text.slice(at);
	}
	const engine = /^[ \t]*RewriteEngine\s+On[ \t]*$/im.exec(body);
	if (!engine) {
		const at = proxy ? start + proxy.index : end;
		return text.slice(0, at) + indent + "RewriteEngine On\n\n" + block + "\n\n" + text.slice(at);
	}
	// Ignore and Allowed IPs first among the rewrite rules, the redirects after those already there
	if (mine <= 1) {
		const at = start + engine.index + engine[0].length;
		return text.slice(0, at) + "\n\n" + block + "\n" + text.slice(at);
	}
	const at = proxy ? start + proxy.index : end;
	return text.slice(0, at) + block + "\n\n" + text.slice(at);
}

function editorReplaceAll(text) {
	// Where the new text starts: the first place it differs from what was there
	const before = editorValue();
	let at = 0;
	while (at < before.length && at < text.length && before[at] === text[at]) at += 1;
	at = text.lastIndexOf("\n", at - 1) + 1;
	if (monacoEditor && !$("#editor-box").hidden) {
		const model = monacoEditor.getModel();
		// One edit, so Ctrl+Z takes the snippet out again
		monacoEditor.executeEdits("snippet", [{ range: model.getFullModelRange(), text }]);
		monacoEditor.pushUndoStop();
	} else {
		$("#editor-text").value = text;
	}
	setDirty(true);
	focusEditor(at);
}

// Back in the editor, with the cursor at offset and that line in view
function focusEditor(offset = null) {
	// After the snippet's window has closed and given the focus back to the menu button
	setTimeout(() => {
		if (monacoEditor && !$("#editor-box").hidden) {
			if (offset !== null) {
				const position = monacoEditor.getModel().getPositionAt(offset);
				monacoEditor.setPosition(position);
				monacoEditor.revealLineInCenterIfOutsideViewport(position.lineNumber);
			}
			monacoEditor.focus();
			return;
		}
		const area = $("#editor-text");
		area.focus();
		if (offset !== null) {
			area.setSelectionRange(offset, offset);
			// A textarea shows the cursor's line when it is given the focus again
			area.blur();
			area.focus();
		}
	}, 0);
}

function snippetKind() {
	const name = (openFile || "").split("/").pop().toLowerCase();
	if (name === "web.config") return "iis";
	if (configurationEditor?.kind === "conf" || (/\.conf$/.test(name) && /sites-(available|enabled)/.test(openFile || ""))) return "apache";
	return "";
}

const snippetLooks = {
	ignore: ["eraser", "Leave chosen paths alone"],
	ips: ["shield", "Only listed addresses reach chosen paths"],
	slash: ["chevron", "Add / to addresses without one"],
	www: ["globe", "Send www.domain to domain"],
	domain: ["external", "Send every other name to one domain"],
	https: ["lock", "Send HTTP to HTTPS"],
	rewrite: ["sync", "Answer one path from another"],
	compression: ["package", "Compress text, JavaScript and JSON"],
	cookies: ["key", "Mark every cookie Secure"],
};
function closeSnippetMenu() {
	$("#editor-snippets-panel").hidden = true;
	$("#editor-snippets-trigger").setAttribute("aria-expanded", "false");
}
function refreshSnippetMenu() {
	// The whole path on hover, when it is too long for the toolbar
	$("#editor-path").title = $("#editor-path").textContent;
	const kind = snippetKind();
	const menu = $("#editor-snippets");
	menu.hidden = !kind;
	closeSnippetMenu();
	if (!kind) return;
	const snippets = kind === "iis" ? iisSnippets : apacheSnippets;
	$("#editor-snippets-panel").replaceChildren(
		el("div", { className: "snippet-panel-head" }, el("span", { textContent: kind === "iis" ? "IIS · WEB.CONFIG" : "APACHE · SITE CONFIG" }), el("strong", { textContent: "Insert a rule" })),
		...Object.entries(snippets).map(([key, snippet]) => {
			const [icon, line] = snippetLooks[key] || ["file", ""];
			return el("button", { type: "button", className: "snippet-item", role: "menuitem", onclick: () => { closeSnippetMenu(); run(() => insertSnippet(key)); } },
				el("span", { className: "snippet-item-icon" }, svgIcon(icon)),
				el("span", { className: "snippet-item-text" }, el("strong", { textContent: snippet.label }), el("small", { textContent: line })));
		}));
}

// Allowed IPs rules already in the file: their name, paths and addresses, and where each one is
const unescapeRegex = (text) => text.replace(/\\(.)/g, "$1");
const sameSet = (a, b) => a.size === b.size && [...a].every((item) => b.has(item));
const lowerSet = (items) => new Set(items.map((item) => item.toLowerCase()));
function allowedIpRules(kind, text) {
	const found = [];
	if (kind === "iis") {
		for (const match of text.matchAll(/<rule\s+name="(Allowed IPs[^"]*)"[\s\S]*?<\/rule>/g)) {
			const paths = /<match\s+url="\^\(\?:([^)]*)\)/.exec(match[0]);
			const addresses = /input="\{REMOTE_ADDR\}"[^>]*pattern="\^\(\?:([^)]*)\)\$"/.exec(match[0]);
			if (!paths || !addresses) continue;
			found.push({ name: match[1], start: match.index, end: match.index + match[0].length,
				paths: lowerSet(paths[1].split("|").map(unescapeRegex)),
				addresses: lowerSet(addresses[1].split("|").map(unescapeRegex).filter((item) => item !== "::1" && item !== "127.0.0.1")) });
		}
	} else {
		for (const match of text.matchAll(/^[ \t]*# (Allowed IPs[^\n]*)\n((?:[ \t]*\S[^\n]*\n?)*)/gm)) {
			const paths = /RewriteRule \^\/\(\?:([^)]*)\)/.exec(match[2]);
			if (!paths) continue;
			found.push({ name: match[1].trim(), start: match.index, end: match.index + match[0].replace(/\n$/, "").length,
				paths: lowerSet(paths[1].split("|").map(unescapeRegex)),
				addresses: lowerSet([...match[2].matchAll(/-R '([^']+)'/g)].map((item) => item[1]).filter((item) => item !== "127.0.0.0/8" && item !== "::1")) });
		}
	}
	return found;
}

// Allowed IPs for paths that already have a rule changes that rule's addresses; other paths get a rule of their own
function placeAllowedIps(kind, text, snippet, values) {
	const rules = allowedIpRules(kind, text);
	const paths = lowerSet(pathsOf(values.paths));
	const addresses = lowerSet(addressesOf(values.addresses, kind === "apache"));
	const same = rules.find((rule) => sameSet(rule.paths, paths));
	if (same) {
		// Written again when anything about it changes: the addresses, what others get, or a rule in an older form
		const rule = snippet.build({ ...values, name: same.name });
		const flat = (value) => value.replace(/\s+/g, " ").trim();
		if (flat(text.slice(kind === "iis" ? same.start : lineStart(text, same.start), same.end)) === flat(rule)) {
			return { text, message: `${same.name} already does exactly this. Nothing was changed.` };
		}
		const indent = lineIndent(text, same.start);
		const replaced = kind === "iis" ? indentBlock(rule, indent).trimStart() : indentBlock(rule, indent).trimStart();
		const start = kind === "iis" ? same.start : lineStart(text, same.start) + indent.length;
		return { text: text.slice(0, start) + replaced + text.slice(same.end),
			message: sameSet(same.addresses, addresses) ? `${same.name} is updated.` : `${same.name} now allows the new addresses.` };
	}
	const taken = new Set(rules.map((rule) => rule.name));
	let name = "Allowed IPs";
	for (let number = 2; taken.has(name); number += 1) name = "Allowed IPs " + number;
	const rule = snippet.build({ ...values, name });
	if (kind === "iis") return { text: insertIisRule(text, name, rule, snippet), message: `${name} inserted. Check it, then save.` };
	const [, ...rest] = rule.split("\n");
	return { text: insertApacheSnippet(text, name, rest.join("\n"), snippet.place), message: `${name} inserted. Check it, then save.` };
}

$("#editor-snippets-trigger").prepend(svgIcon("sliders"));
$("#editor-snippets-trigger").onclick = (event) => {
	event.stopPropagation();
	const panel = $("#editor-snippets-panel");
	panel.hidden = !panel.hidden;
	$("#editor-snippets-trigger").setAttribute("aria-expanded", String(!panel.hidden));
	if (!panel.hidden) panel.querySelector("button")?.focus();
};
editorDialog.addEventListener("click", (event) => { if (!event.target.closest("#editor-snippets")) closeSnippetMenu(); });
// Escape closes the menu first, not the editor
$("#editor-snippets").addEventListener("keydown", (event) => {
	if (event.key === "Escape" && !$("#editor-snippets-panel").hidden) {
		event.preventDefault();
		event.stopPropagation();
		closeSnippetMenu();
		$("#editor-snippets-trigger").focus();
	}
});
async function insertSnippet(key) {
	const kind = snippetKind();
	if (!key || !kind) return;
	const snippet = (kind === "iis" ? iisSnippets : apacheSnippets)[key];
	const answer = await askSnippet(key, snippet, editorValue());
	if (!answer) {
		focusEditor();
		return;
	}
	if (key === "ips") {
		const placed = placeAllowedIps(kind, editorValue(), snippet, answer.values);
		if (placed.text !== editorValue()) editorReplaceAll(placed.text);
		else focusEditor();
		toast(placed.message, "ok");
		return;
	}
	const text = answer.text;
	if (kind === "iis" && snippet.section) {
		editorReplaceAll(insertIisSection(editorValue(), snippet.section, text));
	} else if (kind === "iis") {
		editorReplaceAll(insertIisRule(editorValue(), /name="([^"]+)"/.exec(text)[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&"), text, snippet));
	} else {
		const [first, ...rest] = text.split("\n");
		editorReplaceAll(insertApacheSnippet(editorValue(), first.slice(2), rest.join("\n"), snippet.place));
	}
	toast(`${snippet.label} inserted. Check it, then save.`, "ok");
}


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
	refreshSnippetMenu();
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
	refreshSnippetMenu();
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

	// A user's tasks take no keys: the server ignores them as well, so Ctrl+C cannot leave a task for a shell
	const term = new Terminal({ cursorBlink: currentUser.admin, disableStdin: !currentUser.admin, fontSize: 14, scrollback: 5000, fontFamily: "ui-monospace, Menlo, Consolas, monospace", theme: { background: "#000000" } });
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
	try { currentUser = await api("/api/me"); } catch (error) { /* the page goes on as before */ }
	applyAccess();
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
		sqlCell("Server path", { textContent: backup.path }), sqlCell("Actions", {}, el("div", { className: "actions" }, ...(!canSql(sqlSelected(), backup.database_name, "restore") ? [] : [el("button", {
			textContent: "Restore", disabled: backup.media_count !== 1,
			title: backup.error || (backup.media_count !== 1 ? "Striped or damaged backups are not supported" : "Restore this full backup"),
			onclick: () => run(() => sqlRestore(backup)),
		})]), ...(allowDelete && sqlSnapshot.backup_file_access && canSql(sqlSelected(), backup.database_name, "backup") ? [el("button", {
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
		$("#sql-backup-all").hidden = !hasUserDatabases || !canSql(id, "*", "backup");
		$("#sql-create").hidden = !canSql(id, null, "create");
		// A connection the user did not make is locked for them
		$("#sql-edit-connection").hidden = Boolean(sqlConnections.find((item) => item.id === id)?.locked);
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
				for (const [label, action, need] of [["Backup", "backup", "backup"], ["Copy", "copy", "copy"], ["Delete", "drop", "delete"]]) {
					if (canSql(id, database.name, need)) actions.append(el("button", { textContent: label, onclick: () => run(() => sqlDatabaseAction(action, database)) }));
				}
			}
			if (canSql(id, database.name, "query")) actions.prepend(el("button", { textContent: "Query", onclick: () => openSqlQuery(database) }));
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
	if (currentUser.admin) {
		serverAddress = null;
		const server = await loadServerAddress().catch(() => null);
		preferencesForm.elements.public_address.value = server?.public || "";
		preferencesForm.elements.public_address.placeholder = server?.detected
			? `Found automatically: ${server.detected}` : server?.behind_nat
			? `Behind NAT (${server.address}): the IP or name the internet reaches it on` : "Only behind NAT: the IP or name the internet reaches it on";
	}
	// The start page is a choice only with both Web Server and SQL Server
	$("#preferences-start").hidden = !(currentUser.web && currentUser.sql);
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
		if (currentUser.admin && preferencesForm.elements.public_address.value.trim() !== (serverAddress?.public || "")) {
			await api("/api/preferences", { method: "PUT", json: { public_address: preferencesForm.elements.public_address.value.trim() } });
			serverAddress = null;
		}
		if (keepAliveInstalled && currentUser.admin) {
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
	$("#system-update-reload").hidden = true;
	overlay.classList.remove("failed");
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
				// The admin site restarts during the update: no answer for a while is expected. A timeout is a
				// TimeoutError in Chrome and an AbortError ("Fetch is aborted") in Safari.
				if (!(error instanceof TypeError) && !["TimeoutError", "AbortError"].includes(error.name)) throw error;
				continue;
			}
			if (response.status === 401 || response.redirected) throw new Error("Your session expired. Reload the page and log in to check the update.");
			if ([502, 503, 504].includes(response.status)) continue;
			if (!response.ok) throw new Error("Could not check update status: HTTP " + response.status);
			// While it starts again an answer can be a page rather than the status: asked again a moment later
			let state;
			try { state = await response.json(); } catch (error) { continue; }
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
		$("#system-update-overlay").classList.add("failed");
		$("#system-update-close").hidden = false;
		$("#system-update-reload").hidden = false;
		console.error(error);
	} finally {
		systemUpdating = false;
	}
}
$("#system-update-overlay").addEventListener("cancel", (event) => event.preventDefault());
$("#system-update-close").onclick = () => $("#system-update-overlay").close();
$("#system-update-reload").onclick = () => location.reload();
async function checkSystemUpdate(refresh = false) {
	// Updates of the server are an administrator's: a user's page does not ask
	if (!currentUser.admin) return;
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
for (const [id, icon, tone] of [["menu-users", "users", "green"], ["menu-audit", "clock", "amber"], ["menu-preferences", "sliders", "purple"], ["menu-profile", "user", "blue"], ["menu-logout", "logout", "red"]]) {
	$("#" + id).dataset.tone = tone;
	$("#" + id).prepend(svgIcon(icon));
}
decorate($("#view-sites .toolbar"));
decorate($("#view-files"));
run(() => checkSystemUpdate(true));
run(async () => {
	if (!currentUser.admin) return;
	const state = await api("/api/system-update/run");
	if (state.status === "running") await runSystemUpdate(state);
});
setInterval(() => { if (!document.hidden && !systemUpdating) run(checkSystemUpdate); }, 60000);
document.addEventListener("visibilitychange", () => {
	if (!document.hidden && !systemUpdating) run(() => checkSystemUpdate(true));
});


// ------------------------------------------------------------------ users
// Administrators make users here: a role, and for a user the sites, environments and actions, and SQL Server.
const usersDialog = $("#dlg-users");
const userDialog = $("#dlg-user");
const userForm = $("#form-user");
let usersData = null;
const defaultSiteActions = ["service", "logs", "publish", "sync_data", "sync_settings", "clear_logs"];
const defaultSqlActions = ["query", "backup"];

async function openUsers() {
	usersData = await api("/api/users");
	renderUsers();
	if (!usersDialog.open) usersDialog.showModal();
}
function userSummary(user) {
	if (user.role === "admin") return "Administrator · everything";
	const parts = [];
	if (user.web) parts.push(`${Object.keys(user.sites || {}).length} site${Object.keys(user.sites || {}).length === 1 ? "" : "s"}${user.create_sites ? " · creates sites" : ""}`);
	if (user.sql) parts.push(`${Object.keys(user.connections || {}).length} SQL connection${Object.keys(user.connections || {}).length === 1 ? "" : "s"}`);
	return parts.join(" · ") || "No access yet";
}
function renderUsers() {
	const users = usersData.users;
	$("#users-count").textContent = `${users.length} user${users.length === 1 ? "" : "s"}`;
	$("#users-list").replaceChildren(...(users.length ? users.map((user) => el("button", { type: "button", className: "user-row" + (user.disabled ? " disabled" : ""),
		onclick: () => openUserEditor(user) },
		el("span", { className: "user-avatar", textContent: user.username.slice(0, 1).toUpperCase() }),
		el("span", { className: "user-text" }, el("strong", { textContent: user.username }), el("small", { textContent: userSummary(user) })),
		el("span", { className: "badge " + (user.role === "admin" ? "user-admin" : "user-user"), textContent: user.disabled ? "Locked" : user.role === "admin" ? "Administrator" : "User" }),
		svgIcon("chevron"))) : [el("p", { className: "muted users-empty", textContent: "No other users yet. Add one to give someone their own login." })]));
}

// The site tree: a site's box gives all of it; opened, its environments one by one, and its actions
function siteGrantRow(name, envKeys, grant) {
	const site = allSites.find((item) => item.name === name);
	const label = (key) => {
		const env = site?.environments.find((item) => envKeyOf(item) === key);
		return key === "main" ? `Main site${env ? " · " + (bindingLabel(env) || siteLabel(site)) : ""}` : (env && bindingLabel(env)) || key;
	};
	const chosen = new Set(grant?.envs || []);
	const whole = el("input", { type: "checkbox", className: "grant-site-check" });
	const body = el("div", { className: "grant-body", hidden: !grant });
	const envBoxes = envKeys.map((key) => {
		const box = el("input", { type: "checkbox", value: key, checked: chosen.has(key) });
		box.dataset.env = key;
		return [key, box, el("label", { className: "grant-chip" + (key === "main" ? " main" : "") }, box, el("span", { textContent: label(key) }))];
	});
	const newEnvs = el("input", { type: "checkbox", checked: grant ? Boolean(grant.new_envs) : true });
	const actionBoxes = usersData.site_actions.map(([value, text]) => {
		const box = el("input", { type: "checkbox", value, checked: (grant?.actions || defaultSiteActions).includes(value) });
		return el("label", { className: "grant-chip action" }, box, el("span", { textContent: text }));
	});
	const sync = () => {
		const count = envBoxes.filter(([, box]) => box.checked).length;
		whole.checked = count === envBoxes.length;
		whole.indeterminate = count > 0 && count < envBoxes.length;
		row.classList.toggle("granted", count > 0);
	};
	whole.onchange = () => {
		for (const [, box] of envBoxes) box.checked = whole.checked;
		newEnvs.checked = whole.checked;
		body.hidden = !whole.checked;
		sync();
	};
	for (const [, box] of envBoxes) box.onchange = sync;
	const toggle = el("button", { type: "button", className: "grant-toggle", title: "Environments and actions", ariaLabel: "Environments and actions" }, svgIcon("chevron"));
	toggle.onclick = () => { body.hidden = !body.hidden; toggle.setAttribute("aria-expanded", String(!body.hidden)); };
	body.append(
		el("div", { className: "grant-label", textContent: "Environments" }),
		el("div", { className: "grant-chips" }, ...envBoxes.map(([, , chip]) => chip)),
		...(envKeys.length > 1 ? [el("label", { className: "grant-flag small" }, newEnvs, el("span", { textContent: "Also environments added later" }))] : []),
		el("div", { className: "grant-label", textContent: "May" }),
		el("div", { className: "grant-chips" }, ...actionBoxes));
	const row = el("div", { className: "grant-site" },
		el("div", { className: "grant-head" }, el("label", {}, whole, el("strong", { textContent: site ? siteLabel(site) : name })),
			el("span", { className: "muted", textContent: envKeys.length > 1 ? `${envKeys.length - 1} environment${envKeys.length === 2 ? "" : "s"}` : "" }), toggle),
		body);
	row.dataset.site = name;
	row.collect = () => {
		const envs = envBoxes.filter(([, box]) => box.checked).map(([key]) => key);
		if (!envs.length) return null;
		return { envs, new_envs: newEnvs.checked && envs.length === envKeys.length, actions: actionBoxes.map((chip) => chip.querySelector("input")).filter((box) => box.checked).map((box) => box.value) };
	};
	sync();
	return row;
}

function connectionGrantRow(connection, grant) {
	const box = el("input", { type: "checkbox", checked: Boolean(grant) });
	const chosen = new Set(grant?.databases || ["*"]);
	// Every database, also those made later; or the databases the connection has, one by one
	const everything = el("input", { type: "checkbox", checked: chosen.has("*") });
	const list = el("div", { className: "grant-chips grant-database-list" });
	const actions = usersData.sql_actions.map(([value, text]) => el("label", { className: "grant-chip action" },
		el("input", { type: "checkbox", value, checked: (grant?.actions || defaultSqlActions).includes(value) }), el("span", { textContent: text })));
	let loaded = false;
	const showList = () => { list.hidden = everything.checked; };
	const load = async () => {
		if (loaded) return;
		loaded = true;
		list.replaceChildren(el("span", { className: "muted", textContent: "Loading the databases…" }));
		try {
			const data = await api("/api/sql/" + encodeURIComponent(connection.id));
			const names = data.databases.filter((database) => database.id > 4).map((database) => database.name);
			// A database named in the grant that is gone is still shown, so it can be taken away
			for (const name of chosen) if (name !== "*" && !names.includes(name)) names.push(name);
			list.replaceChildren(...(names.length ? names.sort((a, b) => a.localeCompare(b)).map((name) => el("label", { className: "grant-chip" },
				el("input", { type: "checkbox", value: name, checked: chosen.has(name) }), el("span", { textContent: name })))
				: [el("span", { className: "muted", textContent: "The connection has no databases yet." })]));
		} catch (error) {
			loaded = false;
			list.replaceChildren(el("span", { className: "error", textContent: "The databases could not be read: " + error.message }));
		}
	};
	everything.onchange = showList;
	const body = el("div", { className: "grant-body", hidden: !grant },
		el("div", { className: "grant-label", textContent: "Databases" }),
		el("label", { className: "grant-flag small" }, everything, el("span", { textContent: "Every database, also those made later" })),
		list,
		el("div", { className: "grant-label", textContent: "May" }), el("div", { className: "grant-chips" }, ...actions));
	box.onchange = () => {
		body.hidden = !box.checked;
		row.classList.toggle("granted", box.checked);
		if (box.checked) load();
	};
	const row = el("div", { className: "grant-site" + (grant ? " granted" : "") },
		el("div", { className: "grant-head" }, el("label", {}, box, el("strong", { textContent: connection.name || connection.host })),
			el("span", { className: "muted", textContent: connection.host + (connection.port ? ":" + connection.port : "") })), body);
	showList();
	if (grant) load();
	row.collect = () => {
		if (!box.checked) return null;
		const databases = everything.checked ? ["*"]
			: [...list.querySelectorAll("input:checked")].map((item) => item.value);
		// Names chosen before the list had come are kept as they were
		const kept = loaded ? databases : [...chosen].filter((name) => name !== "*");
		return { databases: everything.checked ? ["*"] : kept,
			actions: actions.map((chip) => chip.querySelector("input")).filter((item) => item.checked).map((item) => item.value),
			...(grant?.owned ? { owned: true } : {}) };
	};
	return row;
}

function openUserEditor(user = null) {
	userForm.reset();
	userForm.dataset.id = user?.id || "";
	$("#user-title").textContent = user ? user.username : "Add user";
	$("#user-description").textContent = user ? "Change what they may do. It counts at once; a new password logs them out." : "They log in with this name and password.";
	$("#user-error").textContent = "";
	userForm.username.value = user?.username || "";
	userForm.password.required = !user;
	userForm.password.placeholder = user ? "Leave empty to keep it" : "At least 12 characters";
	userForm.querySelector(`input[name=role][value=${user?.role || "user"}]`).checked = true;
	userForm.disabled.checked = Boolean(user?.disabled);
	userForm.web.checked = user ? Boolean(user.web) : true;
	userForm.create_sites.checked = Boolean(user?.create_sites);
	userForm.sql.checked = Boolean(user?.sql);
	userForm.manage_connections.checked = Boolean(user?.manage_connections);
	$("#user-sites").replaceChildren(...Object.entries(usersData.sites).sort(([a], [b]) => a.localeCompare(b))
		.map(([name, envs]) => siteGrantRow(name, envs, user?.sites?.[name])));
	$("#user-connections").replaceChildren(...(usersData.connections.length ? usersData.connections.map((connection) => connectionGrantRow(connection, user?.connections?.[connection.id]))
		: [el("p", { className: "muted", textContent: "There are no SQL connections yet." })]));
	$("#user-delete").hidden = !user;
	showUserScope();
	userDialog.showModal();
	userForm.username.focus();
}
function showUserScope() {
	const admin = userForm.role.value === "admin";
	$("#user-scope").hidden = admin;
	$("#user-web").hidden = !userForm.web.checked;
	$("#user-sql").hidden = !userForm.sql.checked;
}
userForm.onchange = (event) => { if (["role", "web", "sql"].includes(event.target.name)) showUserScope(); };
userForm.onsubmit = (event) => {
	event.preventDefault();
	run(async () => {
		const id = userForm.dataset.id;
		const sites = {};
		for (const row of $("#user-sites").children) {
			const grant = row.collect?.();
			if (grant) sites[row.dataset.site] = grant;
		}
		const connections = {};
		const rows = [...$("#user-connections").children].filter((row) => row.collect);
		rows.forEach((row, index) => {
			const grant = row.collect();
			if (grant) connections[usersData.connections[index].id] = grant;
		});
		const body = { username: userForm.username.value.trim(), role: userForm.role.value, disabled: userForm.disabled.checked,
			web: userForm.web.checked, create_sites: userForm.create_sites.checked, sql: userForm.sql.checked,
			manage_connections: userForm.manage_connections.checked, sites, connections };
		if (userForm.password.value) body.password = userForm.password.value;
		try {
			await api(id ? "/api/users/" + encodeURIComponent(id) : "/api/users", { method: id ? "PUT" : "POST", json: body });
		} catch (error) {
			$("#user-error").textContent = error.message;
			return;
		}
		userDialog.close();
		toast(id ? "User saved" : "User added", "ok");
		await openUsers();
	});
};
$("#user-cancel").onclick = () => userDialog.close();
$("#user-delete").onclick = () => run(async () => {
	const name = userForm.username.value;
	if (!(await ask({ title: "Delete user?", message: `${name} can no longer log in. What they made stays. Type ${name} to confirm.`, match: name, danger: true, ok: "Delete" }))) return;
	await api("/api/users/" + encodeURIComponent(userForm.dataset.id), { method: "DELETE" });
	userDialog.close();
	toast("User deleted", "ok");
	await openUsers();
});
$("#users-add").onclick = () => openUserEditor();
$("#users-close").onclick = () => usersDialog.close();
$("#menu-users").onclick = () => { $("#user-menu").hidden = true; run(openUsers); };

// ------------------------------------------------------------------ activity log
const auditDialog = $("#dlg-audit");
let auditEntries = [];
function renderAudit() {
	const words = $("#audit-search").value.trim().toLowerCase();
	const shown = auditEntries.filter((entry) => !words || `${entry.user} ${entry.message} ${entry.ip}`.toLowerCase().includes(words));
	$("#audit-list").replaceChildren(...(shown.length ? shown.map((entry) => el("div", { className: "log-row audit-row" },
		el("span", { className: "log-time", textContent: formatDate(entry.time * 1000) }),
		el("span", { className: "log-source", textContent: entry.user, title: entry.ip }),
		el("span", { className: "log-message", textContent: entry.message }))) : [el("p", { className: "muted logs-empty", textContent: "Nothing yet." })]));
	$("#audit-count").textContent = `${shown.length} of ${auditEntries.length}`;
}
$("#menu-audit").onclick = () => {
	$("#user-menu").hidden = true;
	run(async () => {
		auditEntries = (await api("/api/audit")).entries;
		$("#audit-clear").hidden = !currentUser.root;
		$("#audit-search").value = "";
		renderAudit();
		auditDialog.showModal();
	});
};
$("#audit-search").oninput = renderAudit;
$("#audit-close").onclick = () => auditDialog.close();
$("#audit-clear").dataset.tone = "red";
$("#audit-clear").prepend(svgIcon("eraser"));
// Only root clears the log: what another administrator did stays to be seen. The clearing is the first line after.
$("#audit-clear").onclick = () => run(async () => {
	if (!(await ask({ title: "Clear the activity log?", message: "Every line is deleted. That it was cleared, by whom and when, is written as the first line of the new log.", input: false, ok: "Clear", danger: true }))) return;
	auditEntries = (await api("/api/audit", { method: "DELETE" })).entries;
	renderAudit();
	toast("Activity log cleared", "ok");
});
