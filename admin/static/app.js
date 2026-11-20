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
	"Stop": "service", "Start": "service", "Restart": "service", "Logs": "logs", "Shell": "terminal", "Files": "files",
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
	showServerAddresses();
}
// The server's internal and external address in the header, for administrators. A click copies one.
function privateIPv4(address) {
	return /^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)/.test(address);
}
async function showServerAddresses() {
	const box = $("#server-ips");
	if (!currentUser.admin) {
		box.hidden = true;
		return;
	}
	let data;
	try {
		data = await loadServerAddress();
	} catch {
		return;
	}
	const ipv4 = (data.addresses || []).filter((address) => !address.includes(":"));
	const internal = ipv4.find(privateIPv4) || "";
	// What DNS should point to: the public address set in Preferences, the one found, or a public one of its own
	const external = data.address && !privateIPv4(data.address) ? data.address : "";
	const items = [];
	if (internal && internal !== external) items.push(["Internal", internal]);
	if (external) items.push([internal ? "External" : "IP", external]);
	if (!items.length && ipv4[0]) items.push(["IP", ipv4[0]]);
	// Read again with the access now and then: the buttons stay, so a click that just copied keeps showing it
	const key = JSON.stringify(items);
	if (box.dataset.key === key) return;
	box.dataset.key = key;
	box.replaceChildren(...items.map(([label, address]) => {
		const name = el("span", { className: "server-ip-label", textContent: label });
		const button = el("button", { type: "button", className: "server-ip", title: `${label} address. Click to copy` },
			name, el("span", { className: "server-ip-value", textContent: address }));
		let timer = null;
		button.onclick = () => run(async () => {
			await copyText(address, document.body);
			button.classList.add("copied");
			name.textContent = "Copied";
			clearTimeout(timer);
			timer = setTimeout(() => {
				button.classList.remove("copied");
				name.textContent = label;
			}, 1400);
		});
		return button;
	}));
	box.hidden = !items.length;
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
		const [kind, text] = answer?.local ? ["muted", "Local name"] : !answer ? ["muted", "Could not check"] : answer.here ? ["ok", answer.proxy ? "Via " + answer.proxy : expect ? "Points there" : "Points here"]
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
// expect: the address of another server the names should point to, or a function that gives it as it is typed
function dnsCard(input, { expect: expected = "", name: targetName = "", aliases = null, onState = () => {} } = {}) {
	let expect = "";
	const expectNow = () => (typeof expected === "function" ? expected() : expected) || "";
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
		expect = expectNow();
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
		// An internal address: the names point to a public one in front of it, which cannot be compared from here
		const internal = Boolean(expect) && privateIPv4(expect);
		const pointing = publicNames.filter((answer) => answer?.addresses?.length).length;
		if (!publicNames.length) { status.className = "dns-status muted"; status.textContent = "Local names: no public DNS"; }
		else if (ready) { status.className = "dns-status ok"; status.textContent = expect ? `All names point to ${expect}` : "All names point here"; }
		else if (internal) { status.className = "dns-status muted"; status.textContent = `${pointing} of ${publicNames.length} have a record`; }
		else { status.className = "dns-status warn"; status.textContent = `${here} of ${publicNames.length} point ${expect ? "to " + expect : "here"}`; }
		note.textContent = !expect && server?.behind_nat
			? `The public address could not be found, only an internal one (${address}). Set the public address under Preferences, so the records point to where the internet reaches it.`
			: ready ? ""
			: internal ? `${expect} is an internal address, so the names point to a public address that forwards to it. Harbor cannot see that forwarding from here: the certificate is issued when the names reach the server.`
			: `Make the missing records where each domain's DNS is kept. An HTTPS certificate covers every name, so each must point ${expect ? "to " + expect : "here"}.`;
		note.classList.toggle("warn", Boolean(!expect && server?.behind_nat));
		onState(!publicNames.length ? "local" : ready ? "here" : internal ? "unknown" : "elsewhere");
	};
	const show = async () => {
		expect = expectNow();
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
		// An internal address (behind a router or firewall): the record points to the public address that reaches it,
		// which Harbor cannot see from here
		const internal = Boolean(expect) && privateIPv4(expect);
		records.replaceChildren(record("A", name, internal ? `the public address that reaches ${expect}` : address || "this server's address"),
			...(host && host.includes(".") && host !== name ? [record("CNAME", name, host)] : []));
		note.textContent = !expect && server?.behind_nat
			? `The public address could not be found, only an internal one (${address}). Set the public address under Preferences, so the record points to where the internet reaches it.`
			: internal
				? `${expect} is an internal address, so the name points to a public address that forwards to it. Harbor cannot see that forwarding from here: the certificate is issued when the name reaches the server.`
				: `Make one of these where the domain's DNS is kept. An HTTPS certificate is issued only when the name points ${expect ? "to " + expect : "here"}.`;
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
		if (answer.here) { status.className = "dns-status ok"; status.textContent = answer.proxy ? "Via " + answer.proxy : expect ? `Points to ${expect}` : "Points here"; status.title = answer.proxy ? `Proxied by ${answer.proxy}: its addresses stand in for this server's` : ""; }
		else if (answer.addresses.length && internal) {
			// Not wrong: a public address in front of an internal one cannot be compared from here
			status.className = "dns-status muted"; status.textContent = `Points to ${answer.addresses[0]}`;
			status.title = `${answer.addresses.join(", ")}: public, in front of ${expect}`;
		}
		else if (answer.addresses.length) { status.className = "dns-status warn"; status.textContent = `Points to ${answer.addresses[0]}`; status.title = answer.addresses.join(", "); }
		else { status.className = "dns-status muted"; status.textContent = "No record yet"; }
		onState(answer.here ? "here" : answer.addresses.length ? (internal ? "unknown" : "elsewhere") : "none");
	};
	// Typed, or filled in by the dialog (the binding follows the folder name): checked shortly after it changes
	let seen = null;
	const watch = setInterval(() => {
		if (!document.contains(card)) { clearInterval(watch); return; }
		// A closed dialog hides it, so it is checked again when the dialog opens
		if (!input.offsetParent) { card.hidden = true; seen = null; clearTimeout(timer); return; }
		// Another server's address is checked again when it changes
		const typed = input.value + "\n" + (aliases ? aliases.value : "") + "\n" + expectNow();
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

// The passwords typed in Harbor's dialogs (SSH, Samba, sa, Windows shares) are for a server, not for this site: the
// browser is not to offer to save them. Once a dialog's form is sent (its handler has read the values) and when a
// dialog closes, its password fields are emptied, so there is nothing to save; and password managers are told to keep
// out. The sign-in page is another page: there the browser may keep the login.
// Safari takes any field it sees a password typed into for a password, also a text field that shows dots, and offers to
// save it as the form is sent or as the page is left (a reload). So a dialog's password never stands in a field: the
// field is a plain text field that holds only dots, and what is typed, pasted or deleted in it is kept here (secret),
// which the field's value gives back to the dialogs as before. Safari finds nothing but dots to save.
const VALUE = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
function secretField(input) {
	if (input.dataset.secret) return;
	input.dataset.secret = "1";
	let secret = VALUE.get.call(input) || "";
	const show = () => VALUE.set.call(input, "•".repeat(secret.length));
	input.addEventListener("beforeinput", (event) => {
		const start = input.selectionStart ?? secret.length, end = input.selectionEnd ?? secret.length;
		let next = secret, caret = start;
		const type = event.inputType || "";
		if (type.startsWith("insert")) {
			const text = (event.data ?? event.dataTransfer?.getData("text/plain") ?? "").replace(/[\r\n]/g, "");
			next = secret.slice(0, start) + text + secret.slice(end);
			caret = start + text.length;
		} else if (type.startsWith("delete")) {
			if (start !== end) next = secret.slice(0, start) + secret.slice(end);
			else if (type === "deleteContentBackward") { next = secret.slice(0, Math.max(0, start - 1)) + secret.slice(end); caret = Math.max(0, start - 1); }
			else if (type === "deleteContentForward") next = secret.slice(0, start) + secret.slice(end + 1);
			// A word or a line back (Option or Cmd with Backspace): all before the caret, as there are no words to see
			else if (/Backward/.test(type)) { next = secret.slice(end); caret = 0; }
			else if (/Forward/.test(type)) next = secret.slice(0, start);
		} else if (type === "historyUndo" || type === "historyRedo") {
			event.preventDefault();
			return;
		}
		event.preventDefault();
		secret = next;
		show();
		input.setSelectionRange(caret, caret);
		input.dispatchEvent(new Event("input", { bubbles: true }));
	});
	// Its value is the password for the dialogs; set (to empty, or a password given back) it shows as many dots
	Object.defineProperty(input, "value", { configurable: true, get: () => secret, set: (value) => { secret = String(value ?? ""); show(); } });
	input.form?.addEventListener("reset", () => setTimeout(() => { secret = ""; show(); }));
	show();
}
function maskPasswords(root) {
	for (const input of root.querySelectorAll("input[type=password], input.masked-password")) {
		if (!input.closest("dialog")) continue;
		input.type = "text";
		input.classList.add("masked-password");
		input.setAttribute("autocomplete", "off");
		input.spellcheck = false;
		input.setAttribute("autocapitalize", "off");
		input.setAttribute("autocorrect", "off");
		secretField(input);
	}
}
maskPasswords(document);
new MutationObserver((changes) => {
	for (const change of changes) for (const node of change.addedNodes) if (node.nodeType === 1) maskPasswords(node.matches?.("input") ? node.parentElement || node : node);
}).observe(document.body, { childList: true, subtree: true });
// Safari offers to save a password the moment a form with one is sent, before any of Harbor's code runs. So a dialog's
// form with a password is never sent the browser's way: its submit button and Enter are caught, the browser's own
// checks of the fields run (reportValidity), and the form's submit handlers are run with a submit event of our own,
// which sends nothing. A form of method dialog that its handler leaves be is closed as the browser would.
const holdsPassword = (form) => form?.closest("dialog") && form.querySelector("input[type=password], input.masked-password");
function submitInPlace(form, submitter) {
	if (!form.noValidate && !form.reportValidity()) return;
	const event = typeof SubmitEvent === "function"
		? new SubmitEvent("submit", { bubbles: true, cancelable: true, submitter })
		: new Event("submit", { bubbles: true, cancelable: true });
	const sent = form.dispatchEvent(event);
	if (sent && form.method === "dialog") form.closest("dialog")?.close(submitter?.value || "");
	setTimeout(() => { const dialog = form.closest("dialog"); if (dialog && !dialog.open) forgetPasswords(dialog); });
}
document.addEventListener("click", (event) => {
	const submitter = event.target.closest?.("button, input[type=submit]");
	if (!submitter || !(submitter.type === "submit") || submitter.disabled) return;
	const form = submitter.form;
	if (!holdsPassword(form)) return;
	event.preventDefault();
	submitInPlace(form, submitter);
}, true);
document.addEventListener("keydown", (event) => {
	if (event.key !== "Enter" || event.isComposing || !event.target.matches?.("input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit])")) return;
	const form = event.target.form;
	if (!holdsPassword(form)) return;
	event.preventDefault();
	const submitter = form.querySelector("button[type=submit]:not([disabled]), button:not([type]):not([disabled])");
	if (submitter) submitInPlace(form, submitter);
}, true);
const forgetPasswords = (root) => { for (const input of root.querySelectorAll("input[type=password], input.masked-password")) input.value = ""; };
document.addEventListener("submit", (event) => {
	const dialog = event.target.closest?.("dialog");
	// Only when the dialog is gone: one that stays (two passwords not the same) keeps what was typed
	if (dialog) setTimeout(() => { if (!dialog.open) forgetPasswords(dialog); });
});
document.addEventListener("close", (event) => { if (event.target.matches?.("dialog")) forgetPasswords(event.target); }, true);
document.addEventListener("focusin", (event) => {
	const input = event.target;
	if (!input.matches?.("dialog input[type=password], dialog input.masked-password")) return;
	// "off": new-password is what Safari offers to save, also on a text field
	input.autocomplete = input.classList.contains("masked-password") ? "off" : "new-password";
	for (const name of ["data-1p-ignore", "data-lpignore", "data-bwignore", "data-form-type"]) input.setAttribute(name, name === "data-form-type" ? "other" : "true");
});

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
	// Apache answers 502 or 503 itself while the admin site starts (after an update): no JSON of its own
	if ((response.status === 502 || response.status === 503) && !data.error) throw new Error("The admin site is starting. Try again in a moment.");
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
// Under the Umbraco filter: one major version (e.g. 17), so a selection does not mix sites on different versions
let umbracoMajor = null;
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
		// Environments of sites on another server (managed from there), and the sites that are this server's own
		["managed", "Managed elsewhere", count((s) => !!s.managed_by)],
		["own", "Managed here", count((s) => !s.managed_by)],
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
	// The Umbraco filter splits into its major versions when the sites are on more than one
	const majors = filter === "umbraco" ? umbracoMajors(searched) : [];
	if (!majors.some(([major]) => major === umbracoMajor)) umbracoMajor = null;
	const box = $("#stats");
	box.hidden = listedSites().length <= 1 || (usefulChips.length <= 1 && !pending.length);
	box.replaceChildren();
	const chip = ([key, label, n, title], className = "") => el("div", { className: "chip" + className + (filter === key ? " active" : ""), title: title || "", onclick: () => { filter = key; umbracoMajor = null; renderSites(); } },
		el("b", { textContent: n }), el("span", { textContent: label }));
	for (const item of usefulChips) {
		box.append(chip(item));
		if (item[0] !== "umbraco") continue;
		for (const [major, n] of majors) {
			const dot = el("i", { className: "version-dot" });
			dot.dataset.major = String(major % versionColors);
			box.append(el("div", {
				className: "chip version-chip" + (umbracoMajor === major ? " active" : ""), title: `Only Umbraco ${major} sites (click again for every version)`,
				onclick: () => { umbracoMajor = umbracoMajor === major ? null : major; renderSites(); },
			}, dot, el("b", { textContent: n }), el("span", { textContent: "v" + major })));
		}
	}
	if (pending.length) {
		box.append(el("span", { className: "chip-divider", ariaHidden: "true" }));
		for (const item of pending) box.append(chip(item, " attention"));
	}
}

const umbracoMajorOf = (site) => Number.parseInt(site.umbraco_version, 10);
// [major, number of sites] for the Umbraco sites given, newest first; none when they are all on one version
function umbracoMajors(sites) {
	const counts = new Map();
	for (const site of sites) {
		const major = umbracoMajorOf(site);
		if (site.kind === "umbraco" && Number.isFinite(major)) counts.set(major, (counts.get(major) || 0) + 1);
	}
	return counts.size > 1 ? [...counts].sort((a, b) => b[0] - a[0]) : [];
}

// Filters for sites that need something done
const attentionFilters = {
	pending: ["Publish pending", (s) => s.environments.some((env) => env.publish_status === "pending"),
		"The project changed since an environment was last published"],
	updates: ["Updates available", (s) => s.updates === "available", "NuGet package updates were found"],
	"check-failed": ["Update check failed", (s) => s.updates === "error", "The last package update check failed"],
	"not-set-up": ["Not set up", (s) => !!s.not_set_up, "A folder without a service, or a site whose setup was not finished"],
	missing: ["Missing environment", (s) => s.environments.some(isMissingEnv), "An environment no longer exists on its server"],
	"take-over": ["Awaiting take over", (s) => !!s.windows_source || s.environments.some((env) => env.windows_move),
		"Copied or imported from Windows, which still runs it: take over once its address has moved"],
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
	if (filter === "managed" && !site.managed_by) return false;
	if (filter === "own" && site.managed_by) return false;
	if (kindLabel[filter] && site.kind !== filter) return false;
	if (filter === "umbraco" && umbracoMajor !== null && umbracoMajorOf(site) !== umbracoMajor) return false;
	if (attentionFilters[filter] && !attentionFilters[filter][1](site)) return false;
	return matchesQuery(site);
}


// ------------------------------------------------------------------ button icons
// Inline SVG (Lucide-style, 24x24 stroke icons); the CSP does not allow external images
const iconPaths = {
	grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
	database: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
	hexagon: "M12 2l8.7 5v10L12 22l-8.7-5V7zM12 8v8M8.5 10v4M15.5 10v4",
	dotnet: "M4 7l8-4 8 4v10l-8 4-8-4zM9 10v4M9 10l3 4v-4M15 10h-2v4h2M13 12h1.5",
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
	corner: "M4 4v7a4 4 0 0 0 4 4h12M15 10l5 5-5 5",
	"eye-off": "M9.88 9.88a3 3 0 1 0 4.24 4.24M10.73 5.08A10.4 10.4 0 0 1 12 5c7 0 10 7 10 7a13.2 13.2 0 0 1-1.67 2.68M6.61 6.61A13.5 13.5 0 0 0 2 12s3 7 10 7a9.7 9.7 0 0 0 5.39-1.61M2 2l20 20",
	check: "M20 6 9 17l-5-5",
	windows: "M3 5.6 10 4.6V11H3ZM12 4.3 21 3v8h-9ZM3 13h7v6.4l-7-1ZM12 13h9v8l-9-1.3Z",
	feather: "M20.24 12.24a6 6 0 0 0-8.49-8.49L5 10.5V19h8.5zM16 8 2 22M17.5 15H9",
	download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
	user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
	logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
	sync: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8M21 3v5h-5M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16M8 16H3v5",
	restart: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8M21 3v5h-5",
	undo: "M3 7v6h6M21 17a9 9 0 0 0-15-6.7L3 13",
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
	"SQL Server": ["server", "purple"], "Web server": ["globe", "blue"], "Preferences…": ["sliders", "purple"], "Server tools…": ["package", "purple"], "Tools…": ["package", "purple"], "Backoffice login…": ["key", "purple"], "Move out…": ["undo", "amber"], "Servers…": ["server", "blue"], "Import from Windows…": ["download", "blue"], "Copy from Windows…": ["download", "purple"], "Import sites…": ["download", "blue"], "Copy sites…": ["download", "purple"], "Edit conf": ["pencil", "amber"], "Refresh from Windows…": ["download", "blue"], "Take over…": ["shield", "green"], "Files (/)": ["folder", "blue"],
	"Test config": ["activity", "teal"], "Error logs": ["file", "teal"],
	"Apache error logs": ["file", "teal"], "Who can reach it…": ["shield", "purple"],
	"Copy site": ["file", "blue"],
	"Publish from your machine…": ["upload", "green"],
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

// A phone always shows the lists (of sites, and of a site's environments): it has no buttons to choose, and the
// lists are made for its width. Its choice on the desktop is kept for when the window is wide again.
const phoneWidth = matchMedia("(max-width: 800px)");
// The overview as cards in a grid (the default) or as a list with a row per site, remembered in this browser
let siteLayoutChoice = "grid";
try { if (localStorage.getItem("vns-site-layout") === "list") siteLayoutChoice = "list"; } catch (error) {}
const layoutOf = (choice) => phoneWidth.matches ? "list" : choice;
let siteLayout = layoutOf(siteLayoutChoice);
function applySiteLayout() {
	siteLayout = layoutOf(siteLayoutChoice);
	$("#sites").classList.toggle("list", siteLayout === "list");
	$("#layout-grid").setAttribute("aria-pressed", String(siteLayout === "grid"));
	$("#layout-list").setAttribute("aria-pressed", String(siteLayout === "list"));
}
$("#layout-grid").append(svgIcon("grid"));
$("#layout-list").append(svgIcon("rows"));
for (const layout of ["grid", "list"]) {
	$("#layout-" + layout).onclick = () => {
		siteLayoutChoice = layout;
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
// The Backend button: with the VNS.Harbor package and a chosen Umbraco user it logs straight in with a one-time link
// from Harbor; otherwise, or if that fails, it opens the backoffice's own login
function backendButton(site, env, url, title) {
	return backofficeLogin(openLink("Backend", url, title, true), site, env, url);
}
// A link to the backoffice that logs in as the Backend button does: the button, and the Backoffice link in the details
function backofficeLogin(link, site, env, url) {
	if (!currentUser.admin) return link;
	link.onclick = (event) => {
		event.stopPropagation();
		event.preventDefault();
		// Opened at once, so the browser does not take it for a popup
		const opened = window.open("about:blank", "_blank");
		const target = isTarget(env) ? env.target : "main";
		fetch(`/api/sites/${encodeURIComponent(site.name)}/backoffice-login?target=${encodeURIComponent(target)}`, { headers: { Accept: "application/json" } })
			.then((response) => response.ok ? response.json() : { url: null })
			.catch(() => ({ url: null }))
			.then((data) => {
				if (opened) opened.location = data.url || url;
				else window.open(data.url || url, "_blank", "noopener");
			});
	};
	return link;
}
async function backofficeLoginDialog(site, env) {
	const target = isTarget(env) ? env.target : "main";
	const url = `/api/sites/${encodeURIComponent(site.name)}/backoffice-user?target=${encodeURIComponent(target)}`;
	const current = await api(url);
	const user = await ask({
		title: "Backoffice login",
		message: current.package
			? `The Backend button of ${environmentLabel(site, env)} logs straight in as this Umbraco user, with a link that works once. Leave it empty to open the backoffice's own login.`
			: `${siteLabel(site)} does not have the VNS.Harbor package yet. Add it to the project and publish, then the Backend button can log straight in as this Umbraco user.`,
		value: current.user || "", placeholder: "name@example.com", ok: "Save",
	});
	if (user === null) return;
	await run(async () => {
		await api(url, { json: { user: user.trim() } });
		toast(user.trim() ? "The Backend button logs in as " + user.trim() : "The Backend button opens the login");
	});
}
const backofficeLink = (binding, asButton) => openLink(binding + "/umbraco", "https://" + binding + "/umbraco", "Umbraco backoffice", asButton);
const siteLabel = (site) => site.display_name || site.name;
const bindingLabel = (env) => env.display_binding || env.binding;
// The name people look for. The folder can have another name, and is listed on the page. A site managed from another
// server (an environment of a site there) is named by its folder, as it is there: poda.dk, with the binding www.poda.dk
const siteTitle = (site) => {
	if (site.managed_by) return siteLabel(site);
	const main = site.environments?.find((env) => env.key === "main");
	return (main && bindingLabel(main)) || siteLabel(site);
};
// An environment's folder by its name, where it is a site of its own: on another Linux server (its folder there) or on
// Windows (the last part of its share). One in a folder of the main site here is named by its binding
const envFolderName = (env) => env.mode === "remote" || env.mode === "windows"
	? (env.folder || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "";
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

// Where the site has more environments of the same kind (subsites, each in a folder of its own, as kikayo.dk's),
// a small label with the environment's folder tells them apart: their badges and Frontend buttons are alike. Every
// environment of such a site has it, the main site too, so its rows look alike.
function envFolderLabel(site, env) {
	const kinds = site.environments.map((other) => environmentLabel(site, other));
	if (new Set(kinds).size === kinds.length) return null;
	const folder = envFolderName(env) || (env.folder || env.path || "").replace(/\/+$/, "").split("/").pop() || bindingLabel(env);
	if (!folder) return null;
	const where = env.mode === "windows" ? env.folder : env.mode === "remote" ? `${env.host}:/var/www/${env.folder}` : "/var/www/" + (env.path || env.folder);
	return el("span", { className: "env-folder", textContent: folder, title: "Folder: " + where });
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
	// On a card beside the environment's badge; in the list in a column of its own
	const folder = envFolderLabel(site, env);
	if (folder) (siteLayout === "list" ? row : head).append(folder);
	// The list has room for each environment's state, as its card on the site page shows it
	if (siteLayout === "list") {
		const known = env.unit || (env.mode === "windows" && env.state) || (env.mode === "remote" && env.state && env.state !== "unknown");
		const state = known ? env.state || "unknown" : "";
		const words = { active: "Active", inactive: "Stopped", failed: "Failed", activating: "Starting", deactivating: "Stopping", unknown: "Unknown" };
		row.append(el("span", { className: "env-state", title: env.state_error || "" },
			// A phone shows only the dot: its title says the state
			el("span", { className: "dot " + state, title: state ? words[state] || state : env.mode === "remote" ? "Remote" : "Static" }),
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
		const frontend = openLink("Frontend", address.url, environment + ": " + address.title, true);
		frontend.classList.add("frontend");
		const links = el("div", { className: "site-environment-links" }, frontend);
		if (site.kind === "umbraco") {
			const backend = backendButton(site, env, address.url + "umbraco", environment + " backoffice: " + address.title);
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
			const backend = backendButton(site, env, address.url + "umbraco", environment + " backoffice: " + address.title);
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
		const publishEnvironments = el("button", { textContent: "Publish environments", title: "Publish to every environment, not the main site", onclick: () => run(async () => {
			if (await ask({ title: "Publish environments?", message: `Build the main site and publish it to every environment, one at a time. The main site itself is not published. The services are restarted. If one fails, the rest are not published.${skipped}`, input: false, ok: "Publish environments" })) {
				await openTask({ kind: "publish_both", site: site.name, environments_only: true });
			}
		}) });
		// A phone says Publish: it stands beside the syncs there. Its words are the same, so it keeps its icon.
		publishEnvironments.replaceChildren("Publish", el("span", { className: "desktop-only", textContent: " environments" }));
		targetsActions.append(publishEnvironments);
		publishAll = el("button", { type: "button", textContent: "Publish all", className: "small env-head-action", title: "Publish the main site, then every environment", onclick: () => run(async () => {
			if (await ask({ title: "Publish all?", message: `Publish the main site, then every environment, one at a time. The services are restarted. If one fails, the rest are not published.${skipped}`, input: false, ok: "Publish all" })) {
				await openTask({ kind: "publish_both", site: site.name });
			}
		}) });
	}
	if (site.has_project && site.kind !== "csharp" && site.kind !== "umbraco" && site.scripts.includes("outdated.sh")) {
		actions.append(el("button", { textContent: "Check outdated", title: "outdated.sh", onclick: () => run(() => openTask({ kind: "script", site: site.name, script: "outdated.sh" })) }));
	}
	// Update Python packages: beside the title, as Publish all, and only when pip says some are outdated
	let pythonUpdates = null;
	if (site.kind === "python" && site.scripts.includes("update_packages.sh")) {
		pythonUpdates = el("button", { type: "button", textContent: "Update Python packages", className: "small env-head-action", hidden: true,
			onclick: () => run(async () => {
				// A pinned version (==) in requirements.txt moves too: said, as a new major version may change the app
				if (!(await ask({ title: "Update Python packages?", message: `${pythonUpdates.title}\n\nA version pinned with == in requirements.txt is moved to the new one there. A new major version may change how the app runs: test it after. The service restarts.`, input: false, ok: "Update" }))) return;
				const t = await openTask({ kind: "script", site: site.name, script: "update_packages.sh" });
				// Asked again (not the list kept an hour), so the button goes once all is up to date
				if (t) t.afterTask = () => api(`/api/sites/${encodeURIComponent(site.name)}/python-outdated?refresh=1`).catch(() => {}).then(() => renderSite(site.name));
			}) });
		api(`/api/sites/${encodeURIComponent(site.name)}/python-outdated`).then(({ outdated }) => {
			if (!outdated?.length || !pythonUpdates.isConnected) return;
			pythonUpdates.textContent = `Update Python packages (${outdated.length})`;
			pythonUpdates.title = outdated.map((item) => `${item.name} ${item.version} → ${item.latest}`).join("\n");
			pythonUpdates.hidden = false;
		}).catch(() => { /* no button when pip cannot tell */ });
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
	if (!danger.hidden) danger.append(el("button", { textContent: "Delete everything", className: "danger desktop-only", onclick: () => run(() => deleteSite(site, "all")) }));
	const title = el("div", { className: "detail-title" }, el("h2", { textContent: siteTitle(site) }), kindBadges(site));
	// Set up site… and Move under main site: at the right of the site's card head, as Publish all
	const headActions = [];
	if (site.not_set_up && site.setup_kind !== "python") {
		headActions.push(el("button", { type: "button", textContent: "Set up site…", className: "small env-head-action", onclick: () => run(() => openSetupSiteDialog(site)) }));
	}
	if ((site.adopt_into || []).length) {
		headActions.push(el("button", { type: "button", textContent: "Move under main site", className: "small env-head-action", onclick: () => run(() => openAdoptDialog(site)) }));
	}

	head.append(title);
	if (actions.childElementCount) head.append(actions);
	for (const env of site.environments) {
		if (!isTarget(env)) envs.append(envPanel(site, env));
	}
	// Publish all and Update Python packages: at the right of the main site's card head, as Who can reach it… on the
	// admin site's
	// No buttons beside the site's name: those for the site stand at the right of the main site's card head
	const mainHead = envs.querySelector(".env-head");
	for (const button of [...headActions, publishAll, pythonUpdates]) if (button) (mainHead || title).append(button);
	for (const env of sortedEnvironments(site.environments)) {
		if (!isTarget(env)) continue;
		const panel = envPanel(site, env);
		// What the filter matches: binding, aliases, environment, folder, server
		panel.dataset.search = [env.binding, bindingLabel(env), ...(env.aliases || []), ...aliasLabels(env), env.environment,
			env.folder, env.unit, env.path, env.host, env.target].filter(Boolean).join(" ").toLowerCase();
		if (layoutOf(envLayoutChoice) === "list") envListRow(panel, site.name + "/" + (env.target || "prod"), site, env);
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
	// A phone has no filter (the stylesheet hides it): nothing it was given on a wide screen still hides environments
	search.hidden = targets.childElementCount <= 2;
	if (search.hidden || phoneWidth.matches) search.value = "";
	filterTargets();
	$("#targets-empty").hidden = !(noEnvironments && canAddEnvironment);
	targetsSection.hidden = noEnvironments && !canAddEnvironment;
	if (site.has_project && (site.kind === "csharp" || site.kind === "umbraco")) {
		$("#site-layout").classList.add("with-packages");
		packagesPanel.hidden = false;
		const updates = new Map((site.package_updates || []).map((item) => [item.name.toLowerCase(), item]));
		const packages = (site.packages || []).filter((item) => item.name.toLowerCase() !== "microsoft.icu.icu4c.runtime" || updates.has(item.name.toLowerCase()));
		const packageActions = el("div", { className: "actions package-actions" });
		// Check stands at the right of the head, as Publish all on the main site's card
		const packageCheck = el("button", { type: "button", textContent: "Check", className: "small package-check", title: "Check outdated packages", onclick: () => run(() => openTask({ kind: "bulk", sites: [site.name], action: "check" })) });
		if (updates.size > 1) {
			packageActions.append(el("button", { textContent: "Update all", className: "small primary", onclick: () => run(() => updatePackages(site)) }));
		}
		packageActions.append(el("button", { textContent: "Add", className: "small", title: "Add package", onclick: () => run(() => openPackageDialog(site, "add")) }),
			el("button", { textContent: "Remove", className: "small package-remove", title: "Remove package", onclick: () => run(() => openPackageDialog(site, "remove")) }));
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
		// A phone shows the packages folded: the head unfolds them, and they stay so for the site while the page is open
		const packagesToggle = el("button", { type: "button", className: "env-row-toggle package-toggle", title: "Show the packages", ariaLabel: "Show the packages" }, svgIcon("chevron"));
		const foldPackages = (open) => {
			packagesPanel.classList.toggle("unfolded", open);
			packagesToggle.setAttribute("aria-expanded", String(open));
			if (open) unfoldedPackages.add(site.name); else unfoldedPackages.delete(site.name);
		};
		foldPackages(unfoldedPackages.has(site.name));
		packagesPanel.append(
			el("div", { className: "package-head", onclick: (event) => {
				// Check in the head does not fold the packages
				if (event.target.closest("button:not(.package-toggle)")) return;
				foldPackages(!packagesPanel.classList.contains("unfolded"));
			} },
				el("div", { className: "package-heading" }, el("h3", { textContent: "NuGet packages" }), el("span", { className: "package-count", textContent: String(packages.length), title: "Visible package references" })),
				packageCheck, packagesToggle),
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
		// In the list of environments the dot stands alone: its title says the state
		el("span", { className: "dot " + dot, title: answered || (known ? env.state || "unknown" : env.mode === "windows" ? "windows" : env.mode === "remote" ? "remote" : "static") }),
		el("span", { className: "badge " + (known ? "state-" + (env.state || "unknown") : ""), title: answered, textContent: known ? env.state || "unknown" : env.mode === "windows" ? "windows" : env.mode === "remote" ? "remote" : "static" }));
	const environment = site.kind === "python" ? "Live" : env.environment;
	if (environment) heading.append(el("span", { className: "badge env-" + environment.toLowerCase(), textContent: environment }));
	heading.append(httpsLock(env.https));
	if (isRemoteEnv(env)) heading.append(remoteIcon(env));
	// The admin site: who can reach it, at the right of its card's head
	if (site.admin && !isTarget(env) && currentUser.admin) heading.append(el("button", { type: "button", textContent: "Who can reach it…", className: "small env-head-action", onclick: () => openAdminAccess() }));
	if (missing) heading.append(el("span", { className: "badge env-missing", textContent: "Not found on server" }));
	const pending = publishBadge(env);
	if (pending && !missing) heading.append(el("div", { className: "publish-status" }, pending));

	const scheme = env.scheme || "https";
	const address = environmentAddress(env);
	const binding = !address.url ? bindingLink("") : site.admin && !isTarget(env)
		? el("span", { textContent: bindingLabel(env) })
		: openLink(bindingLabel(env) || address.hostname, address.url, address.title);
	// The main site's name is the page's title. An environment is headed by its folder, as the server it runs on names it
	// (poda.dk); its binding, which can differ (www.poda.dk), is listed below
	if (isTarget(env)) heading.prepend(el("h3", { className: "env-title", textContent: envFolderName(env) || bindingLabel(env) || address.hostname }));
	const meta = el("dl", { className: "meta" }, el("dt", { textContent: "Binding" }), el("dd", {}, binding,
		...(site.answers_any_name && !isTarget(env) ? [el("span", { className: "badge any-name", title: "Apache sends a request for a name that no other site has to this site", textContent: "any name" })] : [])));
	if (env.aliases && env.aliases.length) {
		meta.append(el("dt", { textContent: "Aliases" }), el("dd", { className: "database-name aliases" },
			...env.aliases.flatMap((alias, index) => [
				...(index ? [", "] : []),
				openLink(aliasLabels(env)[index], scheme + "://" + alias + "/"),
			])));
	}
	if (site.kind === "umbraco" && address.url) meta.append(el("dt", { textContent: "Backoffice" }), el("dd", {}, backofficeLogin(openLink((bindingLabel(env) || address.hostname) + "/umbraco", address.url + "umbraco", address.title), site, env, address.url + "umbraco")));
	// A site with no connection in its settings has no datasource to show
	if ((site.kind === "csharp" || site.kind === "umbraco") && env.database) {
		meta.append(el("dt", { textContent: "Datasource" }), el("dd", {
			className: "database-name",
			textContent: env.database + (env.database_server ? ` (${env.database_server.replace(/^\.(?=$|[\\,])/, "localhost")})` : ""),
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
		// A shell on another server runs as root there: an administrator's alone
		if (currentUser.admin) add("Shell", () => openTask({ kind: "remote_tool", site: site.name, tool: "shell", ...targetFields(env) }), "desktop-only");
		add("Files", () => openFiles("", remoteRef(site, env)), "desktop-only");
	} else if (env.mode !== "windows") {
		add("Shell", () => openTask({ kind: "shell", site: site.name, path: env.path, scope: isTarget(env) ? "prod" : "main", ...(isTarget(env) ? targetFields(env) : {}) }), "desktop-only");
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
		(action ? settings : quick).append(el("button", { textContent: label, className: "small", title: file || action, onclick: () => run(async () => {
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
		sync.prepend(el("button", { textContent: "Sync setup…", className: "small sync-setup", title: dataSyncSummary(env), onclick: () => run(() => openDataSyncDialog(site, env)) }));
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
				? openServiceStatus(site, env)
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
	// The main site: to a new folder here, without its environments, or with them to another server (administrators),
	// where it can also be migrated
	if (!isTarget(env) && !site.admin && !site.managed_by) addManage("Copy site", () => copySite(site, env));
	// Each environment has its own Umbraco users, also one on another server or on Windows (its connection is written there)
	// Not on a site managed from another server: its Backoffice login is set where it is managed, and saving it here
	// connected the site to this Harbor, which does not know it (aogtillon.dk lost its sync)
	if (currentUser.admin && site.kind === "umbraco" && !site.managed_by && backofficeLoginFits(site)) addManage("Backoffice login…", () => backofficeLoginDialog(site, env));
	// A site published from a developer's machine, or any C# site that is only a build here (no project, not an
	// environment of a site elsewhere): the script that sends it a new build
	// Python and static sites too: their files are published the same way
	const buildOnly = (!site.has_project && (site.kind === "csharp" || site.kind === "umbraco") || site.kind === "python" || site.kind === "html")
		&& !site.managed_by && !site.admin;
	// Also for a user who may publish it: with their own publish token, no SSH as root
	if ((currentUser.admin || can(site.name, "main", "publish")) && (site.local_publish || buildOnly) && !isTarget(env)) addManage("Publish from your machine…", () => showLocalPublish({
		site: site.name, kind: site.kind, binding: site.local_publish?.binding || bindingLabel(env), https: env.scheme === "https" ? "y" : "n", environment: env.environment || "Production" }));
	// Moved from Windows, here as an environment: its data fetched again, and taken over once the address has moved
	if (currentUser.admin && env.windows_move && isTarget(env)) {
		addManage("Refresh from Windows…", () => refreshMovedFromWindows(site, env));
		addManage("Take over…", () => takeOverMovedFromWindows(site, env));
	}
	// Imported from a live Windows server: its data fetched again, and taken over once the address has moved
	if (currentUser.admin && site.windows_source && !isTarget(env)) {
		addManage("Refresh from Windows…", () => refreshFromWindows(site));
		addManage("Take over…", () => takeOverFromWindows(site));
	}
	// An environment on Windows is only removed here: its IIS site and files stay on the server
	if (hasProd(site) && !site.admin && env.mode === "windows") addManage("Remove environment", () => deleteSite(site, "prod", env), "push-right");
	// An environment is deleted on its own card. The main site only when it has none left: they are built from its
	// project (harbor refuses it too)
	else if (hasProd(site) && !site.admin && isTarget(env)) addManage("Delete site", () => deleteSite(site, "prod", env), "danger");
	if (!hasProd(site) && !site.admin) addManage("Delete site", () => deleteSite(site, "all"), "danger");

	// Its edge in the colour of its environment, as its badge
	const panel = el("section", { className: `env ${isTarget(env) ? "prod" : ""} tone-${environmentLabel(site, env).toLowerCase()}` }, heading, meta);
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
	// Tools that are rarely the point of the row stand to the right: clearing logs, editing files, the syncs, custom scripts,
	// and logs, shell and files beside Stop and Restart. Only with something to their left, so a row of edit
	// buttons alone (on Windows) stays at the left.
	for (const row of [quick, sync, extraTools, actions]) {
		const first = row && [...row.children].find((node) => node.classList.contains("script-menu") || /^(Clear logs|Sync settings…|Sync data…|Edit .*|Logs|Shell|Files)$/.test(node.textContent));
		if (first && first !== row.firstElementChild) first.classList.add("push-right");
	}
	// A phone keeps what is used on the go: publish, the syncs, start or stop, restart, logs, clearing them, checking the
	// service, a certificate, the environment's kind and Remove environment. A site is not deleted from a phone. The rest
	// is on the desktop; rows left without buttons are hidden by the stylesheet.
	for (const node of panel.querySelectorAll(".actions > *")) {
		if (!/^(Publish|Sync setup…|Sync data…|Sync settings…|Start|Stop|Restart|Logs|Clear logs|Check service|Issue certificate|Change environment|Remove environment)$/.test(node.textContent.trim())) node.classList.add("desktop-only");
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

// The environments as cards (the default) or as a list: a row per environment with its Frontend and Backend,
// which unfolds to the whole card with all its buttons. The choice is remembered in this browser, the unfolded rows while the page is open.
let envLayoutChoice = "grid";
try { if (localStorage.getItem("vns-env-layout") === "list") envLayoutChoice = "list"; } catch (error) {}
let envLayout = layoutOf(envLayoutChoice);
const unfoldedEnvs = new Set();
// The sites whose packages are unfolded on a phone, while the page is open
const unfoldedPackages = new Set();
function envListRow(panel, key, site, env) {
	const head = panel.querySelector(".env-head");
	// The environment's addresses, as on the overview's cards: the site, and Umbraco's backoffice
	const address = environmentAddress(env);
	const links = el("div", { className: "env-row-links" });
	if (address.url && !isMissingEnv(env)) {
		// Frontend: the row is named by the address already
		const frontend = openLink("Frontend", address.url, address.title, true);
		frontend.classList.add("frontend");
		links.append(frontend);
		if (site.kind === "umbraco") {
			const backend = backendButton(site, env, address.url + "umbraco", "Backoffice: " + address.title);
			backend.classList.add("backend");
			links.append(backend);
		}
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
	// Something running on it is said with its status line, where there is room: not between its state and environment
	const activity = take((item) => item.classList.contains("site-activity"));
	const status = take((item) => item.classList.contains("dot") || /\bstate-/.test(item.className) || /badge (|static|remote|windows)$/.test(item.className));
	const kind = take((item) => /\benv-(?!missing)/.test(item.className));
	const icons = take((item) => item.classList.contains("https-lock") || item.classList.contains("remote-icon"));
	const note = take((item) => item.classList.contains("publish-status") || item.classList.contains("env-missing"));
	head.replaceChildren(column("env-col-name", title), column("env-col-status", status), column("env-col-kind", kind),
		column("env-col-icons", icons), column("env-col-note", [...activity, ...note, ...parts.filter((item) => !item.dataset.placed)]),
		el("div", { className: "env-col env-col-actions" }, ...(links.childElementCount ? [links] : [])), toggle);
	panel.classList.add("env-listed");
	unfold(unfoldedEnvs.has(key));
}
function applyEnvLayout() {
	envLayout = layoutOf(envLayoutChoice);
	$("#env-targets").classList.toggle("list", envLayout === "list");
	$("#env-layout-grid").setAttribute("aria-pressed", String(envLayout === "grid"));
	$("#env-layout-list").setAttribute("aria-pressed", String(envLayout === "list"));
}
$("#env-layout-grid").append(svgIcon("grid"));
$("#env-layout-list").append(svgIcon("rows"));
for (const layout of ["grid", "list"]) {
	$("#env-layout-" + layout).onclick = () => {
		envLayoutChoice = layout;
		try { localStorage.setItem("vns-env-layout", layout); } catch (error) {}
		const name = currentView === "site" ? decodeURIComponent(location.hash.slice(6)) : "";
		if (name) renderSite(name);
	};
}
applyEnvLayout();
// Turning a tablet, or making the window narrow or wide, draws the lists again in the layout that fits
phoneWidth.addEventListener("change", () => {
	if (!sitesLoaded) return;
	renderSites();
	const name = currentView === "site" ? decodeURIComponent(location.hash.slice(6)) : "";
	if (name) renderSite(name);
});

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
// The admin site's own log is Logs on its page; the menu has Apache's
for (const source of ["apache"]) {
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

// A <select> of servers shown as cards: one per server, the chosen one blue with a tick, and a dashed Add server card
// for that kind, which adds one over the dialog and, once it is added, runs added(name) to fetch the list again and
// choose it. The select stays the form's field: the dialogs read and set it as before, and draw() follows it.
function serverCardsFor(select, kind, added, labelOf = (option) => option.title || "") {
	select.hidden = true;
	select.tabIndex = -1;
	const box = el("div", { className: "server-cards", role: "radiogroup", ariaLabel: kind === "windows" ? "Windows server" : "Linux server" });
	select.after(box);
	const draw = () => box.replaceChildren(...[...select.options].map((option) => el("button", {
		type: "button", className: "server-card" + (option.value === select.value ? " chosen" : ""), role: "radio", ariaChecked: String(option.value === select.value),
		onclick: () => { select.value = option.value; select.dispatchEvent(new Event("change")); },
	}, el("span", { className: "server-card-icon" }, svgIcon(kind === "windows" ? "windows" : "server")),
		el("span", { className: "server-card-text" }, el("strong", { textContent: option.value }), el("small", { textContent: labelOf(option) })),
		el("span", { className: "server-card-check" }, svgIcon("check")))),
	el("button", { type: "button", className: "server-card add", title: `Add a ${kind === "windows" ? "Windows" : "Linux"} server`,
		onclick: () => askAddServer({ kind, after: async (name) => {
			await added();
			if ([...select.options].some((option) => option.value === name)) { select.value = name; select.dispatchEvent(new Event("change")); }
			draw();
		} }) },
	el("span", { className: "server-card-icon" }, el("span", { textContent: "+" })),
	el("span", { className: "server-card-text" }, el("strong", { textContent: "Add server" }), el("small", { textContent: kind === "windows" ? "A Windows share" : "Another Linux server" }))));
	select.addEventListener("change", draw);
	return { draw };
}

// A server from Servers, chosen in a dialog: another server is always one kept there (Add server), never one typed in
// the dialog. Its fields are filled and hidden, and the server (by name) tells the rest. A Windows password stays on
// the server, never in the page. onKept says how many there are, so a dialog offers another server only when there is one.
const serverPickers = [];
// The servers in Servers, as last asked: a DNS check uses a server's public address when its own is internal
let keptServers = [];
const publicAddressOf = (host) => (keptServers.find((item) => item.kind === "linux" && item.host === host)?.public) || "";
function serverPicker(kind, fields, fill, { place = null, heading = null, onKept = null } = {}) {
	// Named by kind: a dialog can have both, and its own field called server (an SQL connection)
	// The servers as cards, one chosen, and a dashed card to add another of this kind. The select behind them is what the
	// form sends, and what the dialogs read and set.
	const select = el("select", { name: kind === "windows" ? "windows_server" : "remote_server", className: "server-picker", hidden: true, tabIndex: -1 });
	const cards = el("div", { className: "server-cards", role: "radiogroup", ariaLabel: kind === "windows" ? "Windows server" : "Server" });
	const row = el("div", { className: "server-picker-row" },
		el("span", { className: "server-picker-label", textContent: kind === "windows" ? "Windows server" : "Server" }), cards, select);
	row.hidden = true;
	let kept = [];
	const drawCards = () => {
		cards.replaceChildren(...kept.map((item) => el("button", {
			type: "button", className: "server-card" + (item.name === select.value ? " chosen" : ""), role: "radio", ariaChecked: String(item.name === select.value),
			onclick: () => { select.value = item.name; select.dispatchEvent(new Event("change")); },
		}, el("span", { className: "server-card-icon" }, svgIcon(kind === "windows" ? "windows" : "server")),
			el("span", { className: "server-card-text" }, el("strong", { textContent: item.name }), el("small", { textContent: item.label })),
			el("span", { className: "server-card-check" }, svgIcon("check")))),
		el("button", { type: "button", className: "server-card add", title: `Add a ${kind === "windows" ? "Windows" : "Linux"} server`,
			onclick: () => askAddServer({ kind, after: async (name) => {
				await picker.refresh();
				if ([...select.options].some((option) => option.value === name)) { select.value = name; select.dispatchEvent(new Event("change")); }
			} }) },
		el("span", { className: "server-card-icon" }, el("span", { textContent: "+" })),
		el("span", { className: "server-card-text" }, el("strong", { textContent: "Add server" }), el("small", { textContent: kind === "windows" ? "A Windows share" : "Another Linux server" }))));
	};
	const choose = () => {
		const chosen = kept.find((item) => item.name === select.value) || null;
		fields.hidden = true;
		if (heading) heading.hidden = true;
		for (const input of fields.querySelectorAll("input")) input.required = false;
		fill(chosen);
		drawCards();
	};
	select.onchange = choose;
	const picker = {
		row,
		async refresh() {
			const all = (await api("/api/servers/kept")).servers;
			keptServers = all;
			kept = all.filter((item) => item.kind === kind);
			select.replaceChildren(...kept.map((item) => el("option", { value: item.name, textContent: `${item.name} (${item.label})` })));
			select.required = Boolean(kept.length);
			row.hidden = !kept.length;
			select.value = kept[0]?.name || "";
			choose();
			if (onKept) onKept(kept.length);
		},
	};
	if (place) place.append(row);
	else fields.before(row);
	serverPickers.push(picker);
	return picker;
}
// A kind of server that Servers does not have yet: its card stays where it is, as a dashed card that opens Add server
// for that kind over the dialog, which stays open. Once it is added, after chooses it there. Choosing the card is not
// possible until there is one: another server is always one in Servers.
function needsServer(card, needed, kind = "linux", after = null) {
	if (!card) return;
	const input = card.querySelector("input");
	const sub = card.querySelector(".tc-sub");
	if (sub.dataset.text === undefined) sub.dataset.text = sub.textContent;
	card.classList.toggle("needs-server", needed);
	input.disabled = needed;
	sub.textContent = needed ? "Add one in Servers first" : sub.dataset.text;
	card.onclick = needed ? (event) => { event.preventDefault(); askAddServer({ kind, after }); } : null;
	card.title = needed ? `Add a ${kind === "windows" ? "Windows" : "Linux"} server` : "";
}
// Add environment offers a Linux or Windows server when Servers has one; else its card says where to add one
function offerProdTarget(target, count) {
	needsServer($("#prod-target-row").querySelector(`input[value=${target}]`)?.closest("label"), !count, target === "windows" ? "windows" : "linux", async (name) => {
		// Added: the card is a card again, chosen, with the new server in its list
		await refreshServerPickers();
		prodForm.elements.target.value = target;
		const select = prodForm.elements[target === "windows" ? "windows_server" : "remote_server"];
		if (select && [...select.options].some((option) => option.value === name)) {
			select.value = name;
			select.dispatchEvent(new Event("change"));
		}
		syncProdForm();
	});
	if (!count && prodForm.elements.target.value === target) {
		prodForm.elements.target.value = "local";
		syncProdForm();
	}
}
const refreshServerPickers = () => Promise.all(serverPickers.map((picker) => picker.refresh())).catch((error) => console.error(error));
{
	const prodForm = $("#dlg-prod form"), copyForm = $("#dlg-copy-site form");
	const set = (form, values) => { for (const [name, value] of Object.entries(values)) if (form.elements[name]) form.elements[name].value = value; };
	// At the top of Add environment, under the kind of server: the rest of the dialog follows from the one chosen
	const sectionOf = (fields) => fields.closest("[id$=-options]").querySelector(".ns-section");
	serverPicker("linux", $("#prod-remote-fields"), (chosen) => {
		if (chosen) set(prodForm, { host: chosen.host, ssh_port: chosen.port, user: chosen.user, password: "" });
		prodForm.elements.host?.dispatchEvent(new Event("input"));
		// The folder and binding follow the server chosen (not what was typed), and DNS is checked against it. Its
		// wildcard domain, if it has one, is asked once: then names go under it
		if (prodForm.dataset.site) applyProdSuggestion();
		if (chosen && !(chosen.name in serverWildcards)) {
			api(`/api/servers/${encodeURIComponent(chosen.name)}/wildcard`).then(({ domain }) => {
				serverWildcards[chosen.name] = domain;
				if (domain && prodForm.dataset.site && prodForm.elements.remote_server?.value === chosen.name) applyProdSuggestion();
			}).catch(() => {});
		}
	}, { place: $("#prod-remote-pick"), heading: sectionOf($("#prod-remote-fields")), onKept: (count) => offerProdTarget("remote", count) });
	serverPicker("windows", $("#prod-windows-fields"), (chosen) => {
		if (chosen) set(prodForm, { smb_share: chosen.share, smb_user: chosen.user, smb_domain: chosen.domain, smb_password: "" });
		prodForm.elements.smb_share?.dispatchEvent(new Event("input"));
		syncProdForm();
	}, { place: $("#prod-windows-pick"), heading: sectionOf($("#prod-windows-fields")), onKept: (count) => offerProdTarget("windows", count) });
	serverPicker("linux", $("#copy-server-fields"), (chosen) => {
		if (chosen) set(copyForm, { host: chosen.host, ssh_port: chosen.port, user: chosen.user, ssh_password: "" });
		copyForm.elements.host?.dispatchEvent(new Event("input"));
	}, { onKept: (count) => {
		// Copy site to another server: only to one in Servers
		const card = $("#copy-server-card");
		// Offered by copySite to an administrator: without a server in Servers its card says where to add one
		if (card.dataset.offered !== "1") return;
		needsServer(card, !count, "linux", async (name) => {
			await refreshServerPickers();
			copyForm.elements.destination.value = "server";
			copyForm.elements.destination[1].dispatchEvent(new Event("change", { bubbles: true }));
			const select = copyForm.elements.remote_server;
			if (select && [...select.options].some((option) => option.value === name)) {
				select.value = name;
				select.dispatchEvent(new Event("change"));
			}
		});
		if (!count && copyForm.elements.destination.value === "server" && !copyForm.elements.destination[0].disabled) {
			copyForm.elements.destination.value = "here";
			copyForm.elements.destination[0].dispatchEvent(new Event("change", { bubbles: true }));
		}
	} });
}

async function copySite(site, main) {
	refreshServerPickers();
	const dialog = $("#dlg-copy-site");
	const form = $("#form-copy-site");
	form.reset();
	$("#copy-site-icon").replaceChildren(svgIcon("file"));
	const field = (name) => form.elements.namedItem(name);
	// Only what the user may do: copying here needs Create sites (the button is left out without it), and only
	// administrators copy or migrate to another server. A copy here leaves the environments out; one to another server takes them along.
	const here = true;
	$("#copy-no-environments").hidden = !hasProd(site);
	const server = currentUser.admin && Boolean(main && (main.unit || main.apache));
	$("#copy-here-card").hidden = !here;
	field("destination")[0].disabled = !here;
	$("#copy-server-card").hidden = !server;
	$("#copy-server-card").dataset.offered = server ? "1" : "";
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
		$("#copy-site-path").textContent = `${field("move_here").checked ? "Moved" : "Copied"} to /var/www/${field("target").value.trim() || "…"}`;
	};
	// A move here is a copy that takes over: only for a site without environments (a copy leaves them out), and only
	// for a user who may delete it. Stopping it while it is copied needs a service.
	const canMoveHere = !hasProd(site) && can(site.name, "main", "delete");
	const hasService = Boolean(main && main.unit);
	$("#copy-move-row").hidden = !canMoveHere;
	field("move_here").disabled = !canMoveHere;
	$("#copy-here-options").hidden = !canMoveHere && !hasService;
	$("#copy-no-environments").textContent = "The copy is a site of its own: its environments are not copied, and you can add environments to it afterwards."
		+ (can(site.name, "main", "delete") ? " So it can be copied here, not moved." : "");
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
	// On the new server it keeps its folder, binding and project unless they are changed. Its environments run the
	// build it makes, so only a site without them can get a new project name.
	field("server_folder").value = siteLabel(site);
	field("server_binding").value = (main && bindingLabel(main)) || siteLabel(site);
	field("server_project").value = site.project || "";
	const serverProject = Boolean(site.project) && !hasProd(site);
	$("#copy-server-project-row").hidden = !serverProject;
	const showServerPath = () => {
		$("#copy-server-path").textContent = `On the new server: /var/www/${field("server_folder").value.trim() || "…"}`;
	};
	// The binding follows the folder while they are the same and the binding is not edited
	let serverBindingEdited = false;
	let previousFolder = field("server_folder").value;
	field("server_folder").oninput = () => {
		if (!serverBindingEdited && field("server_binding").value === previousFolder) field("server_binding").value = field("server_folder").value;
		previousFolder = field("server_folder").value;
		showServerPath();
	};
	field("server_binding").oninput = () => { serverBindingEdited = true; };
	showServerPath();
	// With HTTPS, DNS for the site and its environments here is checked against the new server as it is typed
	$("#copy-server-env-bindings").value = site.environments.filter((env) => isTarget(env) && env.mode === "local").map(bindingLabel).filter(Boolean).join("\n");
	$("#copy-server-dns").replaceChildren(dnsCard(field("server_binding"), { expect: () => publicAddressOf(field("host").value.trim()) || field("host").value.trim(), aliases: $("#copy-server-env-bindings") }));

	const update = () => {
		const toServer = field("destination").value === "server";
		const removing = toServer && field("remove_here").checked;
		$("#copy-here").hidden = toServer;
		$("#copy-here").disabled = toServer;
		$("#copy-server").hidden = !toServer;
		$("#copy-server").disabled = !toServer;
		field("project").required = hasProject && !toServer;
		field("project").disabled = !hasProject || toServer;
		field("server_project").disabled = !serverProject || !toServer;
		const newConnection = hasSqlConnection && field("change_connection").checked;
		sqlFields.hidden = !newConnection;
		sqlFields.disabled = !newConnection;
		$("#copy-delete-row").hidden = !removing;
		field("delete_local").disabled = !removing;
		if (!removing) field("delete_local").checked = false;
		$("#copy-stop-row").hidden = removing;
		const https = toServer && field("https").checked;
		const host = field("host").value.trim();
		$("#copy-server-dns").hidden = !https;
		$("#copy-server-protocol").classList.toggle("secure", https);
		$("#copy-server-protocol").replaceChildren(...(!https
			? [el("strong", { textContent: "Set up with HTTP." }), " DNS may not point to the new server yet, so no certificates are issued there. When it does, use Issue certificate on each site."]
			: [el("strong", { textContent: "Set up with HTTPS." }), privateIPv4(host)
				? ` ${host} is an internal address, so DNS cannot be checked against it here. Each site gets its certificate on the new server; one whose DNS does not point there yet stays on HTTP, and Issue certificate gives it one later.`
				: " Each site gets its certificate on the new server once it runs there. One whose DNS does not point there yet stays on HTTP, and Issue certificate gives it one later."]));
		const movingHere = !toServer && canMoveHere && field("move_here").checked;
		$("#copy-move-delete-row").hidden = !movingHere;
		field("move_delete").disabled = !movingHere;
		if (!movingHere) field("move_delete").checked = false;
		$("#copy-here-stop-row").hidden = movingHere || !hasService;
		field("move_stop").disabled = movingHere || !hasService;
		showCopyPath();
		$("#copy-site-title").textContent = removing ? "Migrate site" : movingHere ? "Move site" : "Copy site";
		$("#copy-site-description").textContent = !toServer
			? movingHere
				? `Move ${siteLabel(site)} to a new folder and binding here. It no longer runs under its old name.`
				: `Copy ${siteLabel(site)} to a new folder here, with its own service and Apache config.`
			: removing ? `Move ${siteLabel(site)} to another server. It no longer runs here.` : `Copy ${siteLabel(site)} to another server. It stays here as it is.`;
		$("#copy-site-submit").textContent = removing ? "Migrate site" : movingHere ? "Move site" : "Copy site";
		for (const item of $("#copy-server-list").querySelectorAll(".goes .move-site-where")) item.textContent = removing ? "Moves" : "Copied";
		$("#copy-server-hint").textContent = "The whole folders go, including builds, media, logs and SQLite databases, so the sites run the same version and nothing is built. A database in SQL Server stays where it is: connections to this server's own SQL Server are pointed at its address, which must accept connections from the new server."
			+ (kept ? ` Environments on other servers are not touched, and are published to${removing ? " from the new server" : " from both servers"}.` : "")
			+ " The new server needs the same Harbor version and CPU architecture, and none of the sites. If a step fails, the sites here run as before.";
	};
	for (const input of field("destination")) input.onchange = update;
	field("remove_here").onchange = update;
	field("move_here").onchange = update;
	field("https").onchange = update;
	field("host").addEventListener("input", update);
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
	field("binding").oninput = () => { bindingEdited = true; };
	field("database").oninput = () => {
		databaseEdited = true;
		suggestUsername();
	};
	field("username").oninput = () => { usernameEdited = true; };
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
					payload = { kind: "move_site", site: site.name, remote_server: field("remote_server").value, host: field("host").value.trim(), ssh_port: field("ssh_port").value,
						user: field("user").value.trim(), password: field("ssh_password").value, copy: !removing,
						folder: field("server_folder").value.trim(), binding: field("server_binding").value.trim(),
						project: serverProject ? field("server_project").value.trim() : "", https: field("https").checked ? "y" : "n",
						delete_local: removing && field("delete_local").checked ? "y" : "n", stop_source: !removing && field("stop_source").checked };
				} else {
					payload = { kind: "copy_site", site: site.name, target: field("target").value.trim(), binding: field("binding").value.trim() };
					if (canMoveHere && field("move_here").checked) {
						payload.remove_here = true;
						payload.delete_local = field("move_delete").checked ? "y" : "n";
					} else if (hasService && field("move_stop").checked) payload.stop_source = true;
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
$("#settings-server-tools").onclick = () => { $("#settings-menu").hidden = true; serverToolsScroll = null; openServerTools(); };
$("#settings-servers").onclick = () => { $("#settings-menu").hidden = true; openServers(); };

// Import from Windows: sites from a live Windows server, set up here on HTTP and kept the same until the address moves
// (harbor/sites/windows_import.py). An administrator's alone.
const windowsDialog = $("#dlg-windows-import");
const windowsForm = $("#form-windows-import");
// A temporary alias under the server the sites go to, as {binding}.<its full name>: filled in until it is typed over
function suggestAlias(input, name) {
	if (input.value && input.value !== input.dataset.suggested) return;
	input.value = name ? `{binding}.${name}` : "";
	input.dataset.suggested = input.value;
}
const windowsKinds = { project: "Project", html: "Static files", build: "Only a build", framework: ".NET Framework (old)", other: "Nothing to run" };
$("#settings-windows-import").onclick = () => {
	$("#settings-menu").hidden = true;
	$("#windows-sites-section").hidden = true;
	$("#windows-sites").replaceChildren();
	$("#windows-import-submit").disabled = true;
	windowsDialog.showModal();
	(windowsForm.elements.share.value ? windowsForm.elements.password : windowsForm.elements.share).focus();
	run(async () => {
		const result = await api("/api/windows/imported");
		renderWindowsImported(result.imported);
		// Under the name this page is reached by (admin.<name>), which reaches this server now, also while it borrows
		// another server's address; else its own full name
		const reached = location.hostname.replace(/^admin\./, "");
		suggestAlias(windowsForm.elements.alias, /^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(reached) && !/^[0-9.]+$/.test(reached) ? reached.toLowerCase() : result.name);
		fillWindowsServers(result.servers);
		showWindowsLogin();
		// The sites are read at once: there is nothing to choose before them
		if (windowsForm.elements.server.value) await listWindowsSites();
	});
};
// The Windows servers kept in Servers, as cards: their share and login are used by harbor, never sent to the page
function fillWindowsServers(servers, chosen = "") {
	const select = windowsForm.elements.server;
	select.replaceChildren(...servers.map((item) => Object.assign(el("option", { value: item.name, textContent: item.name }), { title: item.share })));
	select.required = true;
	select.value = (servers.some((item) => item.name === chosen) ? chosen : servers[0]?.name) || "";
	windowsImportCards.draw();
}
const windowsImportCards = serverCardsFor(windowsForm.elements.server, "windows", async () => {
	fillWindowsServers((await api("/api/windows/imported")).servers, windowsForm.elements.server.value);
});
// A share and login are kept in Servers (Add server, Windows): Import uses one of those, never one typed here
function showWindowsLogin() {
	$("#windows-login-fields").hidden = true;
	for (const input of $("#windows-login-fields").querySelectorAll("input")) input.disabled = true;
}
// Another server chosen: its sites
windowsForm.elements.server.addEventListener("change", () => { showWindowsLogin(); run(listWindowsSites); });
$("#windows-import-cancel").onclick = () => windowsDialog.close();
const windowsLogin = () => windowsForm.elements.server.value ? { server: windowsForm.elements.server.value }
	: Object.fromEntries(["share", "user", "password", "domain"].map((name) => [name, windowsForm.elements[name].value.trim()]));
const windowsChoices = () => [...$("#windows-sites").querySelectorAll(".windows-site:not([hidden]) input[type=checkbox]:not(:disabled)")];
function updateWindowsChoice() {
	for (const box of $("#windows-sites").querySelectorAll(".windows-check")) box.closest(".windows-site").classList.toggle("chosen", box.checked);
	const chosen = windowsChoices().filter((box) => box.checked).length;
	$("#windows-import-submit").disabled = chosen === 0;
	$("#windows-import-submit").textContent = chosen > 1 ? `Import ${chosen} sites` : "Import";
	$("#windows-select-all").textContent = chosen && chosen === windowsChoices().length ? "Select none" : "Select all";
}
$("#windows-select-all").onclick = () => {
	const all = windowsChoices().every((box) => box.checked);
	for (const box of windowsChoices()) box.checked = !all;
	updateWindowsChoice();
};
// The sites on the chosen Windows server, read when the dialog opens and when another server is chosen
async function listWindowsSites() {
	const login = windowsLogin();
	if (!login.server) return;
	$("#windows-sites-section").hidden = true;
	$("#windows-list").hidden = false;
	$("#windows-list").replaceChildren(loadingState(`Reading the sites on ${login.server}…`));
	try {
		const result = await api("/api/windows/list", { json: login });
		// One row per folder: what it holds, and whether it can be imported. Name and note together, the kind in a column of
		// its own, and the binding for what can be imported.
		const rows = result.folders.map((item) => {
			const can = ["project", "html"].includes(item.kind) && !item.imported;
			const notes = [];
			if (item.imported) notes.push(`Imported as ${item.imported}`);
			if (item.umbraco) notes.push(`Umbraco ${item.umbraco}`);
			if (item.kind === "build" && !item.imported) notes.push("An environment of a site with its project: use Copy from Windows");
			if (item.kind === "framework") notes.push("Runs on Windows only (Umbraco 7 and older)");
			if (item.kind === "other") notes.push("Nothing to run here");
			// Name with its kind and notes under it; the fields, each with its label, at the right. A click on the row
			// (not on a field) ticks it.
			const field = (label, control) => el("label", { className: "windows-field" }, el("span", { textContent: label }), control);
			const box = el("input", { type: "checkbox", className: "windows-check", disabled: !can, onchange: () => { row.classList.toggle("chosen", box.checked); updateWindowsChoice(); }, ariaLabel: item.folder });
			const row = el("div", { className: "windows-site" + (can ? "" : " off") },
				box,
				// The kind sits small in the row's top right corner: the name and notes get the room
				el("span", { className: "windows-site-kind " + item.kind, textContent: windowsKinds[item.kind] || item.kind }),
				el("span", { className: "windows-site-text" },
					el("strong", { textContent: item.folder }),
					notes.length ? el("small", { className: "windows-site-meta", textContent: notes.join(" · ") }) : ""),
				can ? el("span", { className: "windows-site-fields" },
					field("Binding", el("input", { className: "windows-site-binding", value: item.folder, spellcheck: false }))) : el("span"));
			row.onclick = (event) => {
				if (!can || event.target.closest("input, select, label")) return;
				box.checked = !box.checked;
				box.onchange();
			};
			row.dataset.folder = item.folder;
			row.dataset.kind = item.imported ? "imported" : item.kind;
			row.dataset.can = can ? "1" : "";
			return row;
		});
		$("#windows-sites").replaceChildren(...rows);
		// The filter: what can be imported at first, by kind, and by name
		const counts = rows.reduce((found, row) => ({ ...found, [row.dataset.kind]: (found[row.dataset.kind] || 0) + 1 }), {});
		const filters = [["can", "Can be imported", rows.filter((row) => row.dataset.can).length], ["all", "All", rows.length],
			...["project", "build", "html", "framework", "imported", "other"].filter((kind) => counts[kind])
				.map((kind) => [kind, kind === "imported" ? "Imported" : windowsKinds[kind], counts[kind]])];
		windowsFilter = "can";
		$("#windows-filter-kinds").replaceChildren(...filters.map(([value, label, count]) => el("button", {
			type: "button", className: "windows-filter-kind" + (value === windowsFilter ? " on" : ""),
			onclick: (event) => {
				windowsFilter = value;
				for (const chip of $("#windows-filter-kinds").children) chip.classList.toggle("on", chip === event.currentTarget);
				applyWindowsFilter();
			} }, `${label} `, el("span", { className: "count", textContent: String(count) }))));
		$("#windows-filter-text").value = "";
		applyWindowsFilter();
		$("#windows-sites-section").hidden = false;
		renderWindowsImported(result.imported);
		updateWindowsChoice();
	} finally {
		$("#windows-list").hidden = true;
	}
}
let windowsFilter = "can";
function applyWindowsFilter() {
	const text = $("#windows-filter-text").value.trim().toLowerCase();
	let shown = 0;
	for (const row of $("#windows-sites").children) {
		const kind = windowsFilter === "all" || (windowsFilter === "can" ? row.dataset.can : row.dataset.kind === windowsFilter);
		row.hidden = !(kind && (!text || row.dataset.folder.toLowerCase().includes(text)));
		shown += row.hidden ? 0 : 1;
	}
	$("#windows-filter-empty").hidden = shown > 0;
	updateWindowsChoice();
}
$("#windows-filter-text").oninput = applyWindowsFilter;
function renderWindowsImported(imported) {
	const box = $("#windows-imported");
	box.hidden = !imported?.length;
	if (!imported?.length) return;
	box.replaceChildren(el("div", { className: "windows-imported-text" },
		el("strong", { textContent: `${imported.length} site${imported.length === 1 ? "" : "s"} imported, not taken over yet` }),
		el("span", { className: "muted", textContent: imported.join(", ") })),
		el("button", { type: "button", textContent: "Refresh all…", onclick: () => refreshFromWindows(null) }));
}
windowsForm.onsubmit = (event) => {
	event.preventDefault();
	const alias = windowsForm.elements.alias.value.trim();
	const sites = [...$("#windows-sites").children].filter((row) => !row.hidden && row.querySelector("input[type=checkbox]")?.checked).map((row) => {
		const binding = row.querySelector(".windows-site-binding").value.trim() || row.dataset.folder;
		return { folder: row.dataset.folder, binding, aliases: alias ? alias.replaceAll("{binding}", binding) : "" };
	});
	if (!sites.length) return;
	run(async () => {
		await openTask({ kind: "windows_import", ...windowsLogin(), sites });
		windowsDialog.close();
	});
};
// Refresh one site (or all) from Windows: the live data fetched again. Pausing takes the site on Windows offline
// (app_offline.htm) while its database is copied.
async function refreshFromWindows(site) {
	const answer = await ask({
		title: site ? `Refresh ${siteLabel(site)} from Windows?` : "Refresh all sites from Windows?",
		message: "The live data is fetched again: the SQLite database, media and uSync (all files of a static site). The site here is stopped for a moment while its database is replaced. Without a pause, the site on Windows goes on running: a database written to just then may need another refresh.",
		input: false, ok: "Refresh", checkbox: "Pause the site on Windows while its database is copied",
	});
	if (!answer) return;
	const pause = Boolean(answer.checked);
	if (windowsDialog.open) windowsDialog.close();
	await openTask(site ? { kind: "windows_refresh", site: site.name, pause } : { kind: "windows_refresh_all", pause });
}
// Copy from Windows: every site's environments on Windows, moved to a Linux server Harbor knows as new environments
// of their sites (harbor/environments/windows_move.py). An administrator's alone.
const windowsMoveDialog = $("#dlg-windows-move");
const windowsMoveForm = $("#form-windows-move");
const windowsMoveChoices = () => [...$("#windows-move-list").querySelectorAll(".windows-site:not([hidden]) .windows-check:not(:disabled)")];
function updateWindowsMoveChoice() {
	for (const box of $("#windows-move-list").querySelectorAll(".windows-check")) box.closest(".windows-site").classList.toggle("chosen", box.checked);
	const chosen = windowsMoveChoices().filter((box) => box.checked).length;
	$("#windows-move-submit").disabled = chosen === 0 || !windowsMoveForm.elements.server.value;
	$("#windows-move-submit").textContent = chosen > 1 ? `Copy ${chosen} environments` : "Copy";
	$("#windows-move-select-all").textContent = chosen && chosen === windowsMoveChoices().length ? "Select none" : "Select all";
}
$("#settings-windows-move").onclick = () => run(async () => {
	$("#settings-menu").hidden = true;
	const result = await api("/api/windows-move");
	fillWindowsMoveServers(result);
	suggestAlias(windowsMoveForm.elements.alias, windowsMoveNames[windowsMoveForm.elements.server.value]);
	const rows = result.environments.map((item) => {
		const can = !item.moved_to;
		const box = el("input", { type: "checkbox", className: "windows-check", disabled: !can, onchange: updateWindowsMoveChoice, ariaLabel: item.binding });
		const row = el("div", { className: "windows-site" + (can ? "" : " off") },
			el("span", { className: "windows-site-kind build", textContent: item.environment || "Environment" }),
			box,
			el("span", { className: "windows-site-text" }, el("strong", { textContent: item.binding || item.target }),
				el("small", { className: "windows-site-meta", textContent: `${item.environment || "Environment"} of ${item.site}` + (item.moved_to ? ` · copied, as ${item.moved_to}` : "") })),
			el("span"));
		row.dataset.site = item.site;
		row.dataset.target = item.target;
		row.dataset.binding = item.binding;
		row.onclick = (event) => {
			if (!can || event.target.closest("input, select, label")) return;
			box.checked = !box.checked;
			updateWindowsMoveChoice();
		};
		return row;
	});
	$("#windows-move-list").replaceChildren(...rows);
	if (!rows.length) $("#windows-move-list").append(el("p", { className: "hint", textContent: "No site here has an environment on Windows." }));
	const moved = result.environments.filter((item) => item.moved_to).length;
	$("#windows-move-moved").hidden = !moved;
	$("#windows-move-moved").replaceChildren(el("div", { className: "windows-imported-text" },
		el("strong", { textContent: `${moved} environment${moved === 1 ? "" : "s"} copied, not taken over yet` }),
		el("span", { className: "muted", textContent: "Their data can be fetched again from Windows" })),
		el("button", { type: "button", textContent: "Refresh all…", onclick: () => refreshMovedFromWindows(null) }));
	$("#windows-move-filter").value = "";
	updateWindowsMoveChoice();
	windowsMoveDialog.showModal();
});
$("#windows-move-filter").oninput = () => {
	const text = $("#windows-move-filter").value.trim().toLowerCase();
	for (const row of $("#windows-move-list").querySelectorAll(".windows-site")) row.hidden = text && !`${row.dataset.binding} ${row.dataset.site}`.toLowerCase().includes(text);
	updateWindowsMoveChoice();
};
let windowsMoveNames = {};
// The Linux servers in Servers, as cards, to copy the environments to
function fillWindowsMoveServers(result, chosen = "") {
	const select = windowsMoveForm.elements.server;
	windowsMoveNames = result.names || {};
	select.replaceChildren(...result.servers.map((name) => Object.assign(el("option", { value: name, textContent: name }), { title: windowsMoveNames[name] || "Linux server" })));
	if (result.servers.includes(chosen)) select.value = chosen;
	windowsMoveCards.draw();
}
const windowsMoveCards = serverCardsFor(windowsMoveForm.elements.server, "linux", async () => {
	fillWindowsMoveServers(await api("/api/windows-move"), windowsMoveForm.elements.server.value);
	updateWindowsMoveChoice();
});
windowsMoveForm.elements.server.onchange = () => {
	suggestAlias(windowsMoveForm.elements.alias, windowsMoveNames[windowsMoveForm.elements.server.value]);
	updateWindowsMoveChoice();
};
$("#windows-move-select-all").onclick = () => {
	const all = windowsMoveChoices().every((box) => box.checked);
	for (const box of windowsMoveChoices()) box.checked = !all;
	updateWindowsMoveChoice();
};
$("#windows-move-cancel").onclick = () => windowsMoveDialog.close();
windowsMoveForm.onsubmit = (event) => {
	event.preventDefault();
	const alias = windowsMoveForm.elements.alias.value.trim();
	const items = windowsMoveChoices().filter((box) => box.checked).map((box) => {
		const row = box.closest(".windows-site");
		return { site: row.dataset.site, target: row.dataset.target, alias: alias ? alias.replaceAll("{binding}", row.dataset.binding) : "" };
	});
	if (!items.length) return;
	run(async () => {
		await openTask({ kind: "windows_move", server: windowsMoveForm.elements.server.value, items });
		windowsMoveDialog.close();
	});
};
async function refreshMovedFromWindows(site, env) {
	const answer = await ask({
		title: env ? `Refresh ${bindingLabel(env) || siteLabel(site)} from Windows?` : "Refresh all moved environments from Windows?",
		message: "The live data is fetched again: the SQLite database, media and uSync. The environment on Linux is stopped for a moment while its database is replaced. Without a pause, the site on Windows goes on running: a database written to just then may need another refresh.",
		input: false, ok: "Refresh", checkbox: "Pause the site on Windows while its database is copied",
	});
	if (!answer) return;
	const pause = Boolean(answer.checked);
	if (windowsMoveDialog.open) windowsMoveDialog.close();
	await openTask(env ? { kind: "windows_move_refresh", site: site.name, target: env.target, pause } : { kind: "windows_move_refresh_all", pause });
}
async function takeOverMovedFromWindows(site, env) {
	if (!(await ask({ title: `Take over ${bindingLabel(env) || siteLabel(site)}?`,
		message: "Do this once the address has moved to the Linux server. The temporary alias is removed, a certificate is issued there, and the environment on Windows is removed from Harbor. Its IIS site and files on Windows are not touched.",
		input: false, ok: "Take over" }))) return;
	await openTask({ kind: "windows_move_take_over", site: site.name, target: env.target });
}
async function takeOverFromWindows(site) {
	if (!(await ask({ title: `Take over ${siteLabel(site)}?`,
		message: "Do this once the address has moved here. The temporary aliases are removed, a certificate is issued, and the site runs on HTTPS. Windows is forgotten, and nothing on it is changed.",
		input: false, ok: "Take over" }))) return;
	await openTask({ kind: "windows_take_over", site: site.name });
}
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
	const name = currentUser.username || $("#menu-user").textContent;
	profileForm.username.value = name;
	profileForm.elements.name.value = currentUser.name || "";
	$("#profile-avatar").textContent = (currentUser.name || name).slice(0, 1).toUpperCase();
	$("#profile-name").textContent = currentUser.name || name;
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
		currentUser = { ...currentUser, username: result.user, name: result.name || "" };
		$("#menu-user").textContent = result.name || result.user;
		$("#btn-user").textContent = (result.name || result.user).slice(0, 1).toUpperCase();
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
	// Published from an external source: a C# site with no project here, so only its environment is asked for
	const csharp = siteForm.type.value === "csharp";
	const fromMachine = csharp && siteForm.elements.external.checked;
	$("#csharp-options").hidden = !csharp;
	for (const field of $("#csharp-options").querySelectorAll(".csharp-here")) field.hidden = fromMachine;
	$("#template-name-option").hidden = !csharp || fromMachine;
	// The dll the site runs: the project published to it later must build the same
	$("#project-label").textContent = fromMachine ? "Project name (its dll)" : "C# project name";
	// The local publish folder follows the environment until it is typed over: _development, _test, …
	const publish = siteForm.elements.publish;
	const suggested = "_" + (siteForm.elements.environment.value || "Development").toLowerCase();
	if (!publish.dataset.edited) publish.value = suggested;
	$("#template-status").hidden = !csharp || fromMachine;
	siteForm.version.disabled = !csharp || fromMachine;
	siteForm.elements.template.disabled = !csharp || fromMachine;
	syncTemplateVersion();
	// The templates are for those who may create sites; the dialog loads them when it opens
	if (csharp && currentUser.create_sites) run(loadDotnetTemplates);
}
siteForm.addEventListener("change", (event) => { if (["type", "external", "environment"].includes(event.target.name)) syncSiteForm(); });
siteForm.elements.publish.addEventListener("input", () => { siteForm.elements.publish.dataset.edited = "1"; });
// Choosing a kind with the mouse moves on to the folder name. Arrow keys between the kinds keep the focus there.
siteForm.addEventListener("click", (event) => {
	if (event.detail > 0 && event.target.closest(".type-card")) setTimeout(() => siteForm.site.focus());
});
siteForm.elements.template.oninput = syncTemplateVersion;
siteForm.site.oninput = () => { if (!siteForm.binding.dataset.edited) siteForm.binding.value = siteForm.site.value; };
siteForm.binding.oninput = () => { siteForm.binding.dataset.edited = "1"; };
// HTTPS as an option card, sent as certificate y or n. It is a choice only when the binding points here: the
// certificate is issued as the site is made. An empty binding is the site for any name, where the server decides.
// The choice comes back when DNS is right. Off at first: HTTPS is chosen, not taken for granted.
function httpsChoice(form) {
	const box = form.elements.https_choice;
	const card = box.closest("label");
	const sub = card.querySelector(".sync-option-sub");
	const text = sub.textContent;
	let chosen = false;
	const send = () => { form.elements.certificate.value = box.checked ? "y" : "n"; };
	box.addEventListener("change", () => {
		chosen = box.checked;
		send();
	});
	const state = (value) => {
		if (value === "checking") return;
		const allowed = value === "here" || value === "empty";
		box.disabled = !allowed;
		card.classList.toggle("disabled", !allowed);
		sub.textContent = allowed ? text : "Possible once DNS for the binding points here. Until then it runs on HTTP, and Issue certificate gives it one later";
		box.checked = allowed && chosen;
		send();
	};
	return { state, reset: () => { chosen = false; state("empty"); } };
}
const siteHttps = httpsChoice(siteForm);
const setupHttps = httpsChoice($("#form-setup-site"));
$("#site-dns").replaceChildren(dnsCard(siteForm.binding, { onState: siteHttps.state }));
$("#setup-site-dns").replaceChildren(dnsCard($("#form-setup-site").binding, { onState: setupHttps.state }));
// The binding's message is about the certificate choice as well: any change clears it, or the browser keeps
// refusing the form before it is checked again
for (const type of ["input", "change"]) siteForm.addEventListener(type, () => siteForm.binding.setCustomValidity(""));
$("#btn-new-site").onclick = () => { siteForm.reset(); siteForm.elements.template.setCustomValidity(""); siteForm.binding.setCustomValidity(""); delete siteForm.binding.dataset.edited; delete siteForm.elements.publish.dataset.edited; syncSiteForm(); dialog.showModal(); siteForm.site.focus(); };
$("#btn-new-site").addEventListener("click", () => siteHttps.reset());
$("#btn-empty-new-site").onclick = () => $("#btn-new-site").click();
$("#site-cancel").onclick = (e) => { e.preventDefault(); dialog.close(); };
siteForm.onsubmit = (event) => {
	event.preventDefault();
	run(async () => {
		const field = siteForm.elements.template;
		if (siteForm.type.value === "csharp" && !siteForm.elements.external.checked) {
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
		if (data.type === "csharp" && data.external) {
			// Made and running now from an empty project's build; the script to publish to it is shown when it is done
			data.kind = "create_site";
			const t = await openTask(data);
			dialog.close();
			const options = { site: data.site.trim(), binding: data.binding, https: data.certificate === "y" ? "y" : "n", environment: data.environment || "Production" };
			if (t) t.afterTask = () => { if (t.taskState !== "failed") showLocalPublish(options); };
			return;
		}
		data.kind = "create_site";
		await openTask(data);
		dialog.close();
	});
};

// The script a developer runs in the project's folder: dotnet publish for Linux, the build to this server over SSH,
// and harbor site receive-build here (harbor/sites/receive_build.py). This server by the name the admin is reached by.
// The script a developer runs in the project's folder: for macOS/Linux (bash) or Windows (cmd), sending over HTTPS with
// their publish token or over SSH as root, for a C# site (dotnet publish for the server's architecture), a Python
// site (its app folder) or a static site (its files). Harbor puts it in place: harbor site receive-build here
// (harbor/sites/receive_build.py)
function localPublishScript({ site, binding, https, environment, kind = "csharp", system = "unix", access = "token", sshHost = "" }) {
	const server = location.hostname.replace(/^admin\./, "");
	// SSH by the server's internal address, where it is usually open; its public name only when it has none
	const ssh = sshHost || server;
	const admin = location.origin;
	const csharp = kind === "csharp" || kind === "umbraco";
	const source = kind === "python" ? "app" : ".";
	return (system === "windows" ? publishCmd : publishBash)({ site, binding: binding || "*", https: https === "y" ? "y" : "n",
		environment: environment || "Production", csharp, source, server, ssh, admin, token: access === "token" });
}
function publishBash({ site, binding, https, environment, csharp, source, server, ssh, admin, token }) {
	const quote = (value) => `'${String(value).replaceAll("'", "'\\''")}'`;
	const lines = [
		"#!/usr/bin/env bash",
		`# Publishes ${site} from this machine to ${server} (VNS Harbor). Run it in the project's folder.`,
		csharp ? "# It builds the project for the server's architecture and sends the build, with a scripts folder beside it, as one"
			: `# It sends the files in ${source === "." ? "this folder" : source} (no .git, node_modules or virtual environment), with a scripts folder beside it, as one`,
		token ? "# tar over HTTPS to Harbor, with your publish token. Harbor checks it, puts it in place"
			: "# tar over one SSH login as root. Harbor checks it, puts it in place",
		csharp || source === "app" ? "# and restarts the site; if it then does not run and answer, the publish fails here too." : "# (a static site has nothing to restart).",
		`# harbor site rollback ${site} on the server puts what it had before back.`,
		"set -euo pipefail",
		`SITE=${quote(site)}`,
		...(token ? [`HARBOR=${quote(admin)}`,
			"# Your publish token (Publish from your machine in Harbor): in HARBOR_TOKEN, or in this file",
			`TOKEN_FILE="$HOME/.config/vns-harbor/${server}.token"`]
			: [`# The server over SSH, by its internal address (SSH is usually open only inside). Give another when you run the`,
				"# script: SERVER=root@name ./publish-harbor.sh. A publish token needs no SSH",
				`SERVER="\${SERVER:-${"root@" + ssh}}"`, 'PORT="${PORT:-22}"']),
		...(csharp ? ["# The project to publish: the only .csproj in this folder, or name it: PROJECT=Site.csproj ./publish-harbor.sh",
			'PROJECT="${PROJECT:-}"']
			: source === "app"
				? ["# The folder whose files are published: app when the project has app/app.py, else this folder (where app.py is).",
					"# Name it to choose: SOURCE=src ./publish-harbor.sh", 'SOURCE="${SOURCE:-}"']
				: [`# The folder whose files are published`, `SOURCE=${quote(source)}`]),
		"# Paths the site keeps on the server from every publish, such as /wwwroot/ or /.env. The first publish to a site",
		"# with files the publish would delete stops until you choose: the paths it keeps, or KEEP=(none) to let them go.",
		"# KEEP=(/wwwroot/)",
		"KEEP=()",
		...(csharp ? ["# y: appsettings.json from the build at every publish (appsettings.<Environment>.json on the server stays).",
			"# n: only the first time, and the server's own stays", "SEND_APPSETTINGS=n"] : []),
		"",
	];
	if (token) lines.push('TOKEN="${HARBOR_TOKEN:-}"',
		'if [ -z "$TOKEN" ] && [ -f "$TOKEN_FILE" ]; then TOKEN="$(cat "$TOKEN_FILE")"; fi',
		'if [ -z "$TOKEN" ]; then echo "No publish token: make one with Publish from your machine in Harbor, and save it in $TOKEN_FILE" >&2; exit 1; fi');
	else lines.push('SSH=(ssh -p "$PORT" -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/harbor-%r@%h:%p" -o ControlPersist=60 "$SERVER")');
	lines.push('OUT="$(mktemp -d)"', "trap 'rm -rf \"$OUT\"' EXIT", 'mkdir -p "$OUT/tar/build"');
	if (csharp) lines.push(
		'if [ -z "$PROJECT" ]; then',
		"\tset -- *.csproj",
		'\tif [ "$#" -ne 1 ] || [ ! -e "$1" ]; then echo "There is not exactly one .csproj here: name it, PROJECT=Site.csproj $0" >&2; exit 1; fi',
		'\tPROJECT="$1"',
		"fi",
		token ? 'ARCH="$(curl -fsS -H "Authorization: Bearer $TOKEN" "$HARBOR/api/publish/$SITE/architecture")"' : 'ARCH="$("${SSH[@]}" uname -m)"',
		'case "$ARCH" in',
		"\tx86_64) RID=linux-x64 ;;",
		"\taarch64|arm64) RID=linux-arm64 ;;",
		'\t*) echo "The server\'s architecture (${ARCH:-not known}) is not one .NET publishes for" >&2; exit 1 ;;',
		"esac",
		'dotnet publish "$PROJECT" -c Release -r "$RID" --self-contained false -o "$OUT/tar/build"');
	else lines.push(
		...(source === "app" ? [
			'if [ -z "$SOURCE" ]; then',
			'\tif [ -f app/app.py ]; then SOURCE=app; elif [ -f app.py ]; then SOURCE=.; fi',
			"fi",
			'if [ -z "$SOURCE" ] || [ ! -f "$SOURCE/app.py" ]; then',
			'\techo "No app.py here or in app/: the site runs app.py (gunicorn app:app). Name the folder: SOURCE=src $0" >&2',
			"\texit 1",
			"fi"] : []),
		'COPYFILE_DISABLE=1 tar -C "$SOURCE" --exclude=./.git --exclude=./node_modules --exclude=./.venv --exclude=./venv --exclude=__pycache__ \\',
		'\t--exclude=./scripts --exclude="./publish-harbor.*" -cf - . | tar -C "$OUT/tar/build" -xf -');
	lines.push(
		'if [ -d scripts ]; then cp -R scripts "$OUT/tar/scripts"; fi',
		'COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"',
		"DIRTY=n",
		'if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then DIRTY=y; fi');
	if (csharp) lines.push("APPSETTINGS=server", 'if [ "$SEND_APPSETTINGS" = y ]; then APPSETTINGS=build; fi');
	if (token) lines.push(
		'COPYFILE_DISABLE=1 tar -C "$OUT/tar" -czf "$OUT/build.tgz" .',
		`QUERY=(--url-query "commit=$COMMIT" --url-query "dirty=$DIRTY"${csharp ? ' --url-query "appsettings=$APPSETTINGS"' : ""})`,
		'for path in ${KEEP[@]+"${KEEP[@]}"}; do QUERY+=(--url-query "keep=$path"); done',
		'curl -sS -N --fail-with-body -X POST -T "$OUT/build.tgz" -H "Authorization: Bearer $TOKEN" "${QUERY[@]}" \\',
		'\t"$HARBOR/api/publish/$SITE" | tee "$OUT/log"',
		'[ "$(tail -n 1 "$OUT/log")" = "Harbor: OK" ]');
	else lines.push(
		`REMOTE="$(printf '%q ' harbor site receive-build "$SITE" --stdin${csharp ? ` --binding ${quote(binding)} --https ${https} --environment ${quote(environment)} --appsettings "$APPSETTINGS"` : ""} \\`,
		'\t--by "$(whoami)@$(hostname -s)" --commit "$COMMIT" --dirty "$DIRTY" ${KEEP[@]+"${KEEP[@]/#/--keep=}"})"',
		'COPYFILE_DISABLE=1 tar -C "$OUT/tar" -czf - . | "${SSH[@]}" "$REMOTE"');
	return lines.join("\n") + "\n";
}
function publishCmd({ site, binding, https, environment, csharp, source, server, ssh, admin, token }) {
	// For the server's shell: one argument in single quotes
	const quote = (value) => `'${String(value).replaceAll("'", "'\\''")}'`;
	const lines = [
		"@echo off",
		"setlocal EnableExtensions EnableDelayedExpansion",
		`rem Publishes ${site} from this machine to ${server} (VNS Harbor). Run it in the project's folder.`,
		csharp ? "rem It builds the project for the server's architecture and sends the build, with a scripts folder beside it, as one"
			: `rem It sends the files in ${source === "." ? "this folder" : source} (no .git, node_modules or virtual environment), with a scripts folder beside it, as one`,
		token ? "rem tar over HTTPS to Harbor, with your publish token. Harbor checks it and puts it in place."
			: "rem tar over SSH as root. Harbor checks it and puts it in place.",
		`rem harbor site rollback ${site} on the server puts what it had before back.`,
		"rem Windows 10 and later have tar, curl and ssh.",
		`set "SITE=${site}"`,
		...(token ? [`set "HARBOR=${admin}"`,
			"rem Your publish token (Publish from your machine in Harbor): in HARBOR_TOKEN, or in this file",
			`set "TOKEN_FILE=%USERPROFILE%\\.config\\vns-harbor\\${server}.token"`]
			: ["rem The server over SSH, by its internal address (SSH is usually open only inside). A publish token needs no SSH",
				`if not defined SERVER set "SERVER=root@${ssh}"`, `if not defined PORT set "PORT=22"`]),
		...(csharp ? ["rem The project to publish: the only .csproj in this folder, or name it here", `set "PROJECT="`]
			: source === "app"
				? ["rem The folder whose files are published: app when the project has app\\app.py, else this folder. Name it to choose", `set "SOURCE="`]
				: ["rem The folder whose files are published", `set "SOURCE=${source}"`]),
		"rem Paths the site keeps on the server from every publish, separated by spaces, such as /wwwroot/ or /.env.",
		"rem The first publish to a site with files the publish would delete stops until you choose: the paths, or none",
		`set "KEEP="`,
		...(csharp ? ["rem y: appsettings.json from the build at every publish (appsettings.<Environment>.json on the server stays)", `set "SEND_APPSETTINGS=n"`] : []),
		"",
	];
	if (token) lines.push(
		`set "TOKEN=%HARBOR_TOKEN%"`,
		`if not defined TOKEN if exist "%TOKEN_FILE%" set /p TOKEN=<"%TOKEN_FILE%"`,
		`if not defined TOKEN (echo No publish token: make one with Publish from your machine in Harbor, and save it in %TOKEN_FILE% & exit /b 1)`);
	lines.push(`set "OUT=%TEMP%\\harbor-publish-%RANDOM%%RANDOM%"`, `mkdir "%OUT%\\tar\\build" || exit /b 1`);
	if (csharp) lines.push(
		"if not defined PROJECT (",
		"\tset /a COUNT=0",
		`\tfor %%f in (*.csproj) do (set /a COUNT+=1 & set "PROJECT=%%f")`,
		`\tif not "!COUNT!"=="1" (echo There is not exactly one .csproj here: name it in PROJECT at the top of this script & goto failed)`,
		")",
		token ? `for /f "usebackq delims=" %%a in (\`curl -fsS -H "Authorization: Bearer !TOKEN!" "%HARBOR%/api/publish/%SITE%/architecture"\`) do set "ARCH=%%a"`
			: `for /f "usebackq delims=" %%a in (\`ssh -p %PORT% %SERVER% uname -m\`) do set "ARCH=%%a"`,
		`if "!ARCH!"=="x86_64" (set "RID=linux-x64") else if "!ARCH!"=="aarch64" (set "RID=linux-arm64") else (echo The server's architecture is not one .NET publishes for & goto failed)`,
		`dotnet publish "!PROJECT!" -c Release -r !RID! --self-contained false -o "%OUT%\\tar\\build" || goto failed`);
	else lines.push(
		...(source === "app" ? [
			`if not defined SOURCE if exist app\\app.py (set "SOURCE=app") else if exist app.py (set "SOURCE=.")`,
			`if not defined SOURCE (echo No app.py here or in app: the site runs app.py, gunicorn app:app. Name the folder in SOURCE & goto failed)`,
			`if not exist "%SOURCE%\\app.py" (echo No app.py in %SOURCE% & goto failed)`] : []),
		`tar -C "%SOURCE%" --exclude=./.git --exclude=./node_modules --exclude=./.venv --exclude=./venv --exclude=__pycache__ --exclude=./scripts --exclude="./publish-harbor.*" -cf "%OUT%\\files.tar" . || goto failed`,
		`tar -C "%OUT%\\tar\\build" -xf "%OUT%\\files.tar" || goto failed`,
		`del "%OUT%\\files.tar"`);
	lines.push(
		`if exist scripts\\ xcopy /e /i /q scripts "%OUT%\\tar\\scripts" >nul`,
		`set "COMMIT="`,
		`for /f %%c in ('git rev-parse --short HEAD 2^>nul') do set "COMMIT=%%c"`,
		`set "DIRTY=n"`,
		`for /f %%d in ('git status --porcelain 2^>nul') do set "DIRTY=y"`);
	if (csharp) lines.push(`set "APPSETTINGS=server"`, `if /i "%SEND_APPSETTINGS%"=="y" set "APPSETTINGS=build"`);
	lines.push(`tar -C "%OUT%\\tar" -czf "%OUT%\\build.tgz" . || goto failed`);
	if (token) lines.push(
		`set "QUERY=commit=!COMMIT!&dirty=!DIRTY!${csharp ? "&appsettings=!APPSETTINGS!" : ""}"`,
		`for %%k in (%KEEP%) do set "QUERY=!QUERY!&keep=%%k"`,
		`curl -sS -N --fail-with-body -X POST -T "%OUT%\\build.tgz" -H "Authorization: Bearer !TOKEN!" "%HARBOR%/api/publish/%SITE%?!QUERY!" > "%OUT%\\log"`,
		`set "CODE=!errorlevel!"`,
		`type "%OUT%\\log"`,
		`set "LAST="`,
		`for /f "usebackq delims=" %%l in ("%OUT%\\log") do set "LAST=%%l"`,
		`if not "!CODE!"=="0" goto failed`,
		`if not "!LAST!"=="Harbor: OK" goto failed`);
	else lines.push(
		`set "KEEPARGS="`,
		`for %%k in (%KEEP%) do set "KEEPARGS=!KEEPARGS! '--keep=%%k'"`,
		`ssh -p %PORT% %SERVER% "harbor site receive-build ${quote(site)} --stdin${csharp ? ` --binding ${quote(binding)} --https ${https} --environment ${quote(environment)} --appsettings !APPSETTINGS!` : ""} --by '%USERNAME%@%COMPUTERNAME%' --commit '!COMMIT!' --dirty !DIRTY!!KEEPARGS!" < "%OUT%\\build.tgz" || goto failed`);
	lines.push(
		`rmdir /s /q "%OUT%"`,
		"echo Published.",
		"exit /b 0",
		":failed",
		`if exist "%OUT%" rmdir /s /q "%OUT%"`,
		"echo The publish failed.",
		"exit /b 1");
	return lines.join("\r\n") + "\r\n";
}
// A token made while the dialog is open: shown until it closes, whichever computer is chosen (it is the same token)
let shownPublishToken = null;
function showLocalPublish(options) {
	const dialog = $("#dlg-local-publish");
	shownPublishToken = null;
	const form = dialog.querySelector("form");
	// The computer guessed from the browser, the access as it was chosen last; both can be changed
	const windows = /win/i.test(navigator.userAgentData?.platform || navigator.platform || "");
	let saved = {};
	try { saved = JSON.parse(localStorage.getItem("harbor-local-publish") || "{}"); } catch (_) { /* none */ }
	form.elements.lp_system.value = saved.system || (windows ? "windows" : "unix");
	form.elements.lp_access.value = saved.access || (currentUser.admin ? "ssh" : "token");
	const render = () => {
		const system = form.elements.lp_system.value;
		const access = form.elements.lp_access.value;
		try { localStorage.setItem("harbor-local-publish", JSON.stringify({ system, access })); } catch (_) { /* not kept */ }
		const file = system === "windows" ? "publish-harbor.cmd" : "publish-harbor.sh";
		const script = localPublishScript({ ...options, system, access });
		const kind = options.kind === "python" ? "Python" : options.kind === "html" ? "static" : "C#";
		$("#local-publish-intro").textContent = kind === "C#" ? `${options.site} runs the build you publish from your computer. Its project must build the same dll.`
			: `${options.site} runs the ${kind} files you publish from your computer.`;
		const step = (...parts) => el("li", {}, ...parts.map((part) => part.code ? el("code", { textContent: part.code }) : part));
		$("#local-publish-steps").replaceChildren(
			step("Save the script as ", { code: file }, " in your project's folder" + (system === "windows" ? "." : ", and make it runnable: "), ...(system === "windows" ? [] : [{ code: `chmod +x ${file}` }])),
			step("Run it there: ", { code: system === "windows" ? file : `./${file}` }, kind === "C#" ? ". It builds the project for this server and sends the build;" : ". It sends the files;",
				" Harbor checks it, puts it in place" + (kind === "static" ? "." : " and checks that the site answers.")));
		$("#local-publish-script").textContent = script;
		$("#local-publish-note").textContent = (kind === "C#" ? "Your computer needs the .NET SDK. " : "")
			+ (access === "token" ? "With a publish token the script publishes over HTTPS as you, to the sites you may publish." : "With SSH it logs in as root with your SSH key.")
			+ " KEEP at the top of the script sets what the site keeps on the server.";
		$("#local-publish-copy").onclick = () => run(async () => { await copyText(script, dialog); toast("Script copied.", "ok"); });
		$("#local-publish-token").hidden = access !== "token";
		if (access === "token") renderPublishToken();
	};
	form.elements.lp_system.forEach((input) => { input.onchange = render; });
	form.elements.lp_access.forEach((input) => { input.onchange = render; });
	render();
	// SSH by the server's internal IPv4 address, as Harbor knows its own addresses
	api("/api/server-address").then(({ addresses }) => {
		const internal = (addresses || []).find((address) => /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(address));
		if (internal && dialog.open) {
			options.sshHost = internal;
			render();
		}
	}).catch(() => { /* its public name, then */ });
	dialog.showModal();
}
// Your publish token, where the script is: whether you have one, a new one shown this once with the command that
// saves it where the script looks, or none. Without one the script uses SSH as root
async function renderPublishToken(made = null) {
	const box = $("#local-publish-token");
	const server = location.hostname.replace(/^admin\./, "");
	const dialog = $("#dlg-local-publish");
	if (made) shownPublishToken = made.token || null;
	const info = made || await api("/api/publish-token").catch(() => ({ exists: false }));
	if (shownPublishToken && info.exists) info.token = shownPublishToken;
	const windows = dialog.querySelector("form").elements.lp_system.value === "windows";
	const when = (value) => value ? new Date(value).toLocaleString() : "never";
	const button = (label, action, className = "") => el("button", { type: "button", className, textContent: label, onclick: () => run(action) });
	const make = async () => {
		if (info.exists && !(await ask({ title: "Make a new publish token?", message: "The token you have stops working: save the new one where you used it.", input: false, ok: "Make new token" }))) return;
		renderPublishToken(await api("/api/publish-token", { method: "POST" }));
	};
	const remove = async () => {
		if (!(await ask({ title: "Remove your publish token?", message: "Publishing with it stops: make a new one, or publish over SSH.", input: false, ok: "Remove", danger: true }))) return;
		shownPublishToken = null;
		renderPublishToken(await api("/api/publish-token", { method: "DELETE" }));
	};
	const head = el("div", { className: "local-publish-token-head" },
		el("span", { className: "local-publish-token-icon", "aria-hidden": "true" }, svgIcon("key")),
		el("div", {}, el("strong", { textContent: "Publish without SSH: your publish token" }),
			el("small", { textContent: info.exists ? `You have one, made ${when(info.made)}, last used ${when(info.used)}. It is shown only when it is made.`
				: "With a token the script publishes over HTTPS to Harbor, as you, to the sites you may publish. Without one it uses SSH as root." })));
	const actions = el("div", { className: "local-publish-token-actions" },
		button(info.exists ? "Make a new token" : "Make a token", make, info.exists ? "" : "primary"),
		...(info.exists ? [button("Remove", remove, "tool-danger")] : []));
	const shown = [];
	if (info.token) {
		// Saved where the script for this computer looks: macOS/Linux in ~/.config, Windows in %USERPROFILE%\.config (Command Prompt)
		const folder = `%USERPROFILE%\\.config\\vns-harbor`;
		const save = windows
			? `if not exist "${folder}" mkdir "${folder}" & echo ${info.token}> "${folder}\\${server}.token"`
			: `mkdir -p ~/.config/vns-harbor && printf '%s' '${info.token}' > ~/.config/vns-harbor/${server}.token && chmod 600 ~/.config/vns-harbor/${server}.token`;
		shown.push(el("p", { className: "hint", textContent: "Your new token. Copy it now: Harbor keeps only a fingerprint of it. "
			+ (windows ? "Run this in Command Prompt (cmd) on your machine to save it where the script looks:" : "Run this on your machine to save it where the script looks:") }),
			el("pre", { className: "local-publish-script local-publish-token-save", textContent: save }),
			button("Copy command", async () => { await copyText(save, dialog); toast("Command copied.", "ok"); }));
	}
	box.replaceChildren(head, actions, ...shown);
}

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
	$("#prod-remote-pick").hidden = !linuxRemote;
	$("#prod-windows-pick").hidden = !windows;
	$("#prod-protocol-hint").textContent = windows
		? "The address checked after publishing. IIS manages the binding and its certificate"
		: "A certificate is issued, so DNS for the binding must point to the server. Off: HTTP without one";
	prodForm.local_path.required = local;
	prodForm.host.required = linuxRemote;
	prodForm.user.required = linuxRemote;
	prodForm.ssh_port.required = linuxRemote;
	prodForm.smb_share.required = windows;
	prodForm.smb_user.required = windows;
	// A kept Windows server has its password with Harbor
	prodForm.smb_password.required = windows && !prodForm.elements.windows_server?.value;
	const moving = Boolean(prodForm.dataset.moveTarget);
	const copying = Boolean(prodForm.dataset.copy);
	const same = target === "same";
	const sameServerMove = moving && !copying && local && prodForm.dataset.moveMode === "local";
	const noSettings = sameServerMove;
	// Only moving to another folder here names it by the folder; anything else has a name of its own
	$("#prod-folder-row").hidden = noSettings;
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
		setProdHttps("n");
	} else if (!otherServer && prodForm.dataset.httpsChoice) {
		setProdHttps(prodForm.dataset.httpsChoice);
		delete prodForm.dataset.httpsChoice;
	}
	$("#prod-protocol-row").parentElement.hidden = noSettings;
	$("#prod-protocol-hint").hidden = noSettings;
	const https = prodForm.elements.https.value === "y";
	if (otherServer && !windows) {
		$("#prod-protocol-hint").textContent = https
			? "It gets its certificate on the new server once it runs there. If the binding does not point there yet, it stays on HTTP, and Issue certificate gives it one later."
			: "It starts with HTTP, as DNS may not point to the new server yet. Choose HTTPS when it does, or use Issue certificate on it later.";
	}
	// With HTTPS on Linux, DNS for the binding is checked against the server it runs on
	// Shown whether or not HTTPS is chosen: whether the name points to the server is the first thing to know
	$("#prod-dns").hidden = noSettings || windows;
	prodForm.folder.required = !noSettings;
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
		: moving ? "A new subfolder of this site's folder, or a new folder directly under /var/www, which then also becomes the site's name."
		: "Use a subfolder of this site's folder, or /var/www/<production folder> to keep it outside the main site's folder.";
	const previous = prodForm.dataset.target;
	prodForm.dataset.target = target;
	if (previous && previous !== target) applyProdSuggestion();
	gateProdHttps();
}
// What the dialog suggests for a new environment or a copy: its folder, binding and folder here. On this server it is
// <environment>-<site>; on another server Production keeps the site's own name and binding, the others get their own.
// A name with this server's name in it, as on another server: umb18.linux-01.vns.dk on linux-02 is umb18.linux-02.vns.dk.
// This server's name is the one its admin is reached by (admin.linux-01.vns.dk), the other's from Servers.
// Each server's wildcard domain, by name, as asked (kept while the page is open: the lists of servers are fetched again)
const serverWildcards = {};
function onServer(name, server) {
	// A wildcard on the server's own domain (*.kikayo.dk to it): the site's own part under it, umb18.kikayo.dk
	const here = location.hostname.replace(/^admin\./, "").toLowerCase();
	if (server?.wildcard && name.toLowerCase().endsWith("." + here)) return name.slice(0, -here.length) + server.wildcard;
	const own = location.hostname.replace(/^admin\./, "").split(".")[0].toLowerCase();
	const other = String(server?.fqdn || server?.name || "").split(".")[0].toLowerCase();
	if (!own || !other || own === other || !/^[a-z0-9-]+$/.test(other)) return name;
	return name.replace(new RegExp(`(^|\\.)${own.replace(/[-]/g, "\\-")}(?=\\.|$)`, "i"), `$1${other}`);
}
function prodSuggestion() {
	const target = prodForm.elements.target.value;
	const environment = (prodForm.elements.environment.value || "Production").toLowerCase();
	const label = prodForm.dataset.siteLabel;
	const own = `${environment}-${label}`;
	// Only on another server: on this one, or the same one as the environment copied, the name is taken
	const sameName = (target === "remote" || target === "windows") && environment === "production";
	// On another Linux server the names follow its name, where they have this server's name in them
	const found = target === "remote" ? keptServers.find((item) => item.kind === "linux" && item.name === prodForm.elements.remote_server?.value) : null;
	const server = found ? { ...found, wildcard: serverWildcards[found.name] || "" } : null;
	const follow = (name) => server ? onServer(name, server) : name;
	return { folder: follow(sameName ? label : own), binding: follow(sameName ? prodForm.dataset.mainBinding : own),
		// Here, in the main site's folder: _development, _test, _staging or _production
		path: `/var/www/${prodForm.dataset.siteName}/_${environment}` };
}
// A field that still holds the last suggestion takes the new one; what was entered stays. A migration keeps its names.
function applyProdSuggestion() {
	if (prodForm.dataset.moveTarget && !prodForm.dataset.copy) return;
	const next = prodSuggestion();
	const last = prodForm.dataset;
	if (last.suggestedFolder === undefined || prodForm.folder.value === last.suggestedFolder) prodForm.folder.value = next.folder;
	if (last.suggestedBinding === undefined || (!prodForm.binding.dataset.edited && prodForm.binding.value === last.suggestedBinding)) prodForm.binding.value = next.binding;
	if (last.suggestedPath === undefined || prodForm.local_path.value === last.suggestedPath) prodForm.local_path.value = next.path;
	Object.assign(last, { suggestedFolder: next.folder, suggestedBinding: next.binding, suggestedPath: next.path });
}
function forgetProdSuggestion() {
	for (const key of ["suggestedFolder", "suggestedBinding", "suggestedPath"]) delete prodForm.dataset[key];
}
function openCreateProdDialog(site, moveEnv, copying = false) {
	refreshServerPickers();
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
	prodForm.dataset.siteName = site.name;
	prodForm.dataset.siteLabel = siteLabel(site);
	prodForm.dataset.mainBinding = binding;
	forgetProdSuggestion();
	prodForm.elements.target.value = moveEnv?.mode === "remote" ? "same" : "local";
	setProdHttps(main && main.scheme === "http" ? "n" : "y");
	prodMoveContext = moveEnv ? { site, moveEnv, binding } : null;
	if (moveEnv) {
		// The environment first: a copy's suggested name comes from it
		if (["Development", "Test", "Staging", "Production"].includes(moveEnv.environment)) prodForm.elements.environment.value = moveEnv.environment;
		applyProdMoveMode();
		setProdHttps(moveEnv.scheme === "http" ? "n" : "y");
	}
	// Another environment on a Windows server gives the share and login it was set up with
	const windowsEnv = !moveEnv && site.environments.find((env) => env.mode === "windows" && env.folder);
	if (windowsEnv) {
		prodForm.smb_share.value = windowsEnv.folder.split("/").slice(0, -1).join("/");
		if (windowsEnv.user) prodForm.smb_user.value = windowsEnv.user;
	}
	prodForm.dataset.target = prodForm.elements.target.value;
	// A new environment starts with the suggestions; a copy got them above
	if (!moveEnv) applyProdSuggestion();
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
	// A copy is a new environment and is suggested names of its own; a migration keeps those it has
	forgetProdSuggestion();
	if (copying) {
		delete prodForm.binding.dataset.edited;
		applyProdSuggestion();
		return;
	}
	const name = moveEnv.unit || moveEnv.folder || siteLabel(site);
	prodForm.folder.value = name;
	prodForm.binding.value = bindingLabel(moveEnv) || binding;
	prodForm.binding.dataset.edited = "1";
	prodForm.local_path.value = `/var/www/${name}`;
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
// HTTPS is an option card; the form sends https y or n, as the select it replaced did
// HTTPS as in New site: a choice only once DNS for the binding points to the server the environment runs on (on
// Windows IIS has the binding and its certificate). What was chosen comes back when DNS is right.
let prodHttpsWanted = true;
let prodDnsState = "empty";
function gateProdHttps() {
	const box = prodForm.elements.https_card;
	const card = box.closest("label");
	const allowed = prodForm.elements.target.value === "windows" || prodDnsState === "here";
	box.disabled = !allowed;
	card.classList.toggle("disabled", !allowed);
	if (!allowed) $("#prod-protocol-hint").textContent = "Possible once DNS for the binding points to the server. Until then it runs on HTTP, and Issue certificate gives it one later";
	box.checked = allowed && prodHttpsWanted;
	prodForm.elements.https.value = box.checked ? "y" : "n";
}
function setProdHttps(value) {
	prodHttpsWanted = value === "y";
	gateProdHttps();
}
prodForm.elements.https_card.addEventListener("change", () => {
	prodHttpsWanted = prodForm.elements.https_card.checked;
	prodForm.elements.https.value = prodHttpsWanted ? "y" : "n";
	syncProdForm();
});
$("#prod-dns").replaceChildren(dnsCard(prodForm.binding, { onState: (state) => {
	if (state === "checking") return;
	prodDnsState = state;
	gateProdHttps();
}, expect: () => {
	const target = prodForm.elements.target.value;
	const host = target === "remote" ? prodForm.host.value.trim() : target === "same" ? prodForm.dataset.moveHost || "" : "";
	// A server in Servers behind a router: DNS points to its public address
	return publicAddressOf(host) || host;
} }));
// The suggestions follow the environment, on this server or another
$("#prod-environment-row").onchange = applyProdSuggestion;
prodForm.folder.oninput = () => {
	if (!prodForm.binding.dataset.edited) prodForm.binding.value = prodForm.folder.value;
	syncProdForm();
};
// The absolute path never changes the binding: it is the folder, not the site's address
prodForm.local_path.oninput = syncProdForm;
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
	for (const key of ["conn_server", "conn_database", "conn_username", "conn_password", "change_connection", "remove_here", "https_card"]) delete data[key];
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
	setupHttps.reset();
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
// Move under main site: the site and where it goes, the main sites as cards (one chosen), and what it means in three points
function openAdoptDialog(site) {
	adoptForm.dataset.site = site.name;
	const select = $("#adopt-main");
	select.replaceChildren(...site.adopt_into.map((name) => el("option", { value: name, textContent: name })));
	select.value = site.adopt_into[0] || "";
	const env = (site.environments || []).find((item) => !isTarget(item));
	const emblem = (kind) => el("span", { className: "server-card-icon" }, svgIcon(kind === "python" ? "hexagon" : kind === "html" ? "globe" : "dotnet"));
	const draw = () => {
		$("#adopt-from").replaceChildren(
			el("div", { className: "adopt-site" }, emblem(site.kind), el("span", { className: "server-card-text" },
				el("strong", { textContent: siteLabel(site) }), el("small", { textContent: env ? bindingLabel(env) || "This site" : "This site" }))),
			el("span", { className: "adopt-arrow" }, svgIcon("corner")),
			el("div", { className: "adopt-site to" }, emblem((allSites.find((item) => item.name === select.value) || {}).kind), el("span", { className: "server-card-text" },
				el("strong", { textContent: select.value || "—" }), el("small", { textContent: "Its main site" }))));
		$("#adopt-mains").replaceChildren(...site.adopt_into.map((name) => {
			const main = allSites.find((item) => item.name === name) || { name, environments: [] };
			const count = (main.environments || []).filter(isTarget).length;
			return el("button", { type: "button", className: "server-card" + (name === select.value ? " chosen" : ""), role: "radio", ariaChecked: String(name === select.value),
				onclick: () => { select.value = name; draw(); } },
				emblem(main.kind), el("span", { className: "server-card-text" }, el("strong", { textContent: siteLabel(main) }),
					el("small", { textContent: count ? `${count} environment${count === 1 ? "" : "s"} already` : "No environments yet" })),
				el("span", { className: "server-card-check" }, svgIcon("check")));
		}));
	};
	draw();
	adoptDialog.showModal();
}
$("#adopt-cancel").onclick = () => adoptDialog.close();
for (const [index, icon] of ["play", "upload", "undo"].entries()) $("#dlg-adopt").querySelectorAll(".adopt-point-icon")[index]?.append(svgIcon(icon));
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
	// Only a single destination is chosen for you: of several, you choose
	if (cards.length === 1) cards[0].querySelector("input").checked = true;
	$("#sync-select-all").hidden = cards.length < 2;
	syncStatus = null;
	syncForced = false;
	$("#sync-settings-first").checked = false;
	$("#sync-settings-first-row").hidden = true;
	$("#sync-anyway-row").hidden = true;
	$("#sync-recent").hidden = true;
	updateSyncSelection();
	syncDialog.showModal();
	// When each was last synced, and what has not been sent: as the dashboard in Umbraco shows it
	const asked = syncForm.dataset.source + syncForm.dataset.data;
	api(`/api/sites/${encodeURIComponent(site.name)}/sync-status?source=${encodeURIComponent(syncForm.dataset.source)}&kind=${data ? "data" : "settings"}`)
		.then((status) => {
			if (!syncDialog.open || syncForm.dataset.source + syncForm.dataset.data !== asked) return;
			syncStatus = status;
			applySyncStatus(true);
		})
		.catch((error) => console.error(error));
}
let syncStatus = null;
let syncForced = false;
const syncKindNames = { settings: "Settings", content: "Content", media: "Media" };
function syncAgo(time) {
	const seconds = Math.max(0, (Date.now() - new Date(time).getTime()) / 1000);
	if (seconds < 60) return "just now";
	if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
	if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
	if (seconds < 172800) return "yesterday";
	if (seconds < 30 * 86400) return `${Math.round(seconds / 86400)} days ago`;
	return formatDate(time);
}
// Up to date: every kind synced there, and the source has not changed since
const syncUpToDate = (info) => Object.values(info?.history || {}).every((found) => found.last?.ok === true && found.changed === false)
	&& Object.keys(info?.history || {}).length > 0;
// Each card gets its status; what is up to date, or whose settings are behind, is not chosen unless asked for
function applySyncStatus(choose = false) {
	if (!syncStatus) return;
	const first = $("#sync-settings-first").checked;
	let anyCurrent = false, anyBehind = false;
	for (const input of syncChoices()) {
		const card = input.closest("label");
		const info = syncStatus.destinations[input.value];
		card.querySelector(".sync-status")?.remove();
		if (!info) continue;
		const current = syncUpToDate(info);
		const behind = info.settings_behind || "";
		anyCurrent ||= current;
		anyBehind ||= Boolean(behind);
		const rows = Object.entries(info.history).map(([kind, found]) => {
			const last = found.last;
			const [state, text] = !last ? ["never", "Never synced"] : !last.ok ? ["failed", "Last sync failed"]
				: found.changed === true ? ["changed", "Changes not synced"] : found.changed === false ? ["current", "Up to date"] : ["unknown", ""];
			return el("span", { className: "sync-status-row " + state, title: last ? `${formatDate(last.time)}${last.who ? " by " + last.who : ""}` : "" },
				el("span", { className: "sync-status-kind", textContent: syncKindNames[kind] || kind }),
				el("span", { className: "sync-status-when", textContent: last ? syncAgo(last.time) : "" }),
				text ? el("span", { className: "sync-status-pill", textContent: text }) : "");
		});
		if (behind) rows.push(el("span", { className: "sync-status-row behind" }, el("span", { className: "sync-status-pill", textContent: first ? "Settings go first" : "Sync settings first" })));
		card.append(el("span", { className: "sync-status" }, ...rows));
		card.title = behind && !first ? `${behind}. Data only goes to a site with the same document types.` : "";
		const blocked = (current && !syncForced) || (behind && !first);
		card.classList.toggle("up-to-date", current && !syncForced);
		card.classList.toggle("settings-behind", Boolean(behind) && !first);
		input.disabled = Boolean(blocked);
		if (blocked) input.checked = false;
	}
	// A single destination is chosen when it has something to sync; of several, you choose
	if (choose && syncChoices().length === 1 && !syncChoices().some((input) => input.checked)) {
		const open = syncChoices().find((input) => !input.disabled);
		if (open) open.checked = true;
	}
	$("#sync-anyway-row").hidden = !anyCurrent || syncForced;
	$("#sync-settings-first-row").hidden = !syncForm.dataset.data || !anyBehind;
	renderSyncRecent();
	updateSyncSelection();
}
function renderSyncRecent() {
	const recent = syncStatus?.recent || [];
	const box = $("#sync-recent");
	box.hidden = !recent.length;
	const kinds = (list) => list.map((kind) => syncKindNames[kind] || kind).join(", ");
	box.replaceChildren(el("div", { className: "sync-recent-title", textContent: "Recent syncs" }),
		el("table", {}, ...recent.slice(0, 5).map((entry) => el("tr", { className: entry.ok ? "" : "failed" },
			el("td", { className: "muted", textContent: syncAgo(entry.time), title: formatDate(entry.time) }),
			el("td", { textContent: `${entry.from} → ${entry.to}` }),
			el("td", { textContent: kinds(entry.kinds || []) }),
			el("td", { className: "muted", textContent: entry.who || "" }),
			el("td", { className: "sync-recent-result", textContent: entry.ok ? "✓" : "Failed" })))));
}
$("#sync-anyway").onclick = () => { syncForced = true; applySyncStatus(true); };
$("#sync-settings-first").onchange = () => applySyncStatus(true);
$("#sync-cancel").onclick = () => syncDialog.close();
const syncChoices = () => [...$("#sync-destination").querySelectorAll("input[name=destination]")];
function updateSyncSelection() {
	const chosen = syncChoices().filter((input) => input.checked).length;
	const all = chosen === syncChoices().filter((input) => !input.disabled).length;
	$("#sync-select-all").textContent = all ? "Select none" : "Select all";
	const verb = syncForm.dataset.data ? "Sync data" : "Sync";
	$("#sync-submit").textContent = chosen > 1 ? `${verb} to ${chosen} sites` : verb;
	$("#sync-submit").disabled = chosen === 0;
}
$("#sync-select-all").onclick = () => {
	const open = syncChoices().filter((input) => !input.disabled);
	const all = open.every((input) => input.checked);
	for (const input of open) input.checked = !all;
	updateSyncSelection();
};
syncForm.onsubmit = (event) => {
	event.preventDefault();
	const destination = syncChoices().filter((input) => input.checked).map((input) => input.value);
	if (!destination.length) return;
	const data = { kind: syncForm.dataset.data ? "data_sync" : "sync_env", site: syncForm.dataset.site, source: syncForm.dataset.source, destination,
		...(syncForm.dataset.data && $("#sync-settings-first").checked ? { settings_first: true } : {}) };
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
		both: "Publish each main site, then all its environments. Four sites run at a time.",
		outdated: "Run dotnet outdated --upgrade for every selected project. Review changes and publish afterward.",
		clear_logs: "Delete the Umbraco logs of each selected main site. Sites that are not Umbraco are skipped.",
		clear_logs_prod: "Delete the Umbraco logs of every environment of the selected sites. Sites that are not Umbraco, or have no environments, are skipped.",
		clear_logs_all: "Delete the Umbraco logs of each selected main site and all its environments. Sites that are not Umbraco are skipped.",
		sync_settings: "Each main site sends its settings (uSync, Views and assets) to its environments. An environment that does not take settings in Sync setup is left out.",
		sync_data: "Each main site sends its content and media to its environments. Content is added and updated, never deleted. An environment that does not take content and media in Sync setup is left out.",
		sync_all: "Each main site sends its settings to its environments where they are behind, then its content and media. What an environment does not take in Sync setup is left out.",
	}[action] || "";
	const packageAction = action === "add" || action === "remove";
	$("#multi-package-fields").hidden = !packageAction;
	multiForm.elements.namedItem("package").disabled = !packageAction;
	$("#multi-version-row").hidden = action !== "add";
	multiForm.elements.namedItem("version").disabled = action !== "add";
	const selected = listedSites().filter((site) => multiSites.has(site.name));
	const clearing = action.startsWith("clear_logs");
	const syncing = action.startsWith("sync_");
	// Publish all on a site with only its main site publishes the main site
	const mainOnly = action === "both" ? selected.filter((site) => !site.scripts.includes("publish_prod.sh")) : [];
	const skipped = action === "prod"
		? selected.filter((site) => !site.scripts.includes("publish_prod.sh"))
		: action === "clear_logs_prod" || syncing ? selected.filter((site) => site.kind !== "umbraco" || !site.environments.some(isTarget))
		: clearing ? selected.filter((site) => site.kind !== "umbraco") : [];
	$("#multi-skipped").textContent = skipped.length
		? `Will be skipped (${clearing || syncing ? (action === "clear_logs" || action === "clear_logs_all" ? "not Umbraco" : "not Umbraco, or no environments") : "no environments"}): ` + skipped.map(siteLabel).join(", ")
		: mainOnly.length ? "Only the main site is published (no environments): " + mainOnly.map(siteLabel).join(", ") : "";
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
	// Clearing logs and sync are for Umbraco sites
	const umbraco = listedSites().some((site) => multiSites.has(site.name) && site.kind === "umbraco");
	$("#multi-maintenance").hidden = !umbraco;
	$("#multi-sync").hidden = !umbraco;
	// Each choice and section with its icon, once
	for (const holder of multiDialog.querySelectorAll("[data-icon]")) {
		const target = holder.matches(".multi-choice") ? holder.querySelector(".tc-icon") : holder;
		if (target && !target.firstChild) target.append(svgIcon(holder.dataset.icon));
	}
	syncMultiAction();
	multiDialog.showModal();
	// The dialog, not its first choice: nothing looks chosen before a choice is made
	multiDialog.focus();
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
// The site the open file belongs to, when known: Umbraco's settings are only offered for an Umbraco site's file, other sites get the general ones
let openFileSite = null;
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

// Monaco is downloaded into static/vendor by harbor admin install/update; without it a plain textarea is used
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
		{ name: "paths", label: "Paths to leave alone", umbraco: "umbraco/api, umbraco/surface, umbraco/backoffice", placeholder: "api, webhooks", hint: "Separate them with spaces or commas." }] },
	ips: { description: "Only these addresses reach the site, or the paths chosen. This server itself always does.", fields: [
		{ name: "addresses", label: "Allowed IP addresses", placeholder: "192.0.2.10, 198.51.100.7", hint: "Separate them with spaces or commas.", yourAddress: true },
		// Choices as segments: [value, title, line under it, only for this kind of config]
		{ name: "scope", label: "Protect", options: [["paths", "Chosen paths", "Only the paths below"], ["site", "The whole site", "Certificate renewal still gets through"]] },
		{ name: "paths", label: "Paths to protect", umbraco: "umbraco", placeholder: "admin, private", showIf: ["scope", "paths"],
			umbracoHint: "Add app_plugins only if no package serves visitors files from it: Umbraco Forms does, and its forms would stop working." },
		{ name: "response", label: "Others get", options: [["forbidden", "403", "Forbidden"], ["notfound", "404", "As if it was not there"], ["redirect", "Redirect", "Sent somewhere else"], ["abort", "Closed", "No answer at all", "iis"]] },
		{ name: "target", label: "Redirect to", value: "/", showIf: ["response", "redirect"], hint: "A path on the site, like /, or a whole address.",
			// The whole site closed: a path on it is closed too, and would send the visitor round and round
			wholeSite: { placeholder: "https://www.google.dk/", hint: "An address on another site: every path on this one is closed." } }] },
	domain: { description: "Every other name the site answers on is sent to this one.", fields: [
		{ name: "domain", label: "Domain", placeholder: "example.dk", hint: "Always to https://. A whole address, like https://example.dk, works too." },
		{ name: "status", label: "Redirect", options: [["permanent", "Permanent", "301: browsers and search engines remember it"], ["temporary", "Temporary", "302: nothing is remembered"]] }] },
	// Without fields: the header says what it does, and Insert puts it in
	slash: { description: "Addresses without a / at the end are sent to the same address with one. Files, /umbraco and /media are left alone.", fields: [] },
	www: { description: "Visitors on www.domain are sent to the domain without www, on https.", fields: [] },
	https: { description: "Visitors on http:// are sent to the same address on https://.", fields: [] },
	compression: { description: "Text, CSS, JavaScript, JSON and SVG are sent compressed, so pages load faster.", fields: [] },
	cookies: { description: "Every cookie the site sets is marked Secure: browsers only send it back over https.", fields: [] },
	rewrite: { description: "Visitors ask for the first path and get what the site has at the second. The address they see does not change.", fields: [
		{ name: "from", label: "From", placeholder: "old/page" },
		{ name: "to", label: "To", placeholder: "new/page" }] },
};

// Where a redirect for Allowed IPs may go: a path on the site, or an http(s) address. For the whole site only an
// address elsewhere: a path on it would be closed too, and send the visitor round and round.
function redirectTargetOf(value, wholeSite = false) {
	const target = String(value || "").trim() || "/";
	if (wholeSite && !/^https?:\/\/[^\s"'<>]+$/.test(target)) throw new Error("The whole site is closed: redirect to an address elsewhere, like https://example.dk/.");
	if (!/^(\/[^\s"'<>]*|https?:\/\/[^\s"'<>]+)$/.test(target)) throw new Error("Redirect to a path like / or an address like https://example.dk/.");
	return target;
}
// Allowed IPs for the whole site leaves Let's Encrypt's challenge open, or the site's certificate could not be renewed
const wholeSite = (v) => v.scope === "site";
const ACME_IIS = "^(?!\\.well-known/acme-challenge/)";
const ACME_APACHE = "RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/";

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
  <match url="${wholeSite(v) ? ACME_IIS : `^(?:${xmlEscape(pathPattern(pathsOf(v.paths)))})(?:/|$)`}" />
  <conditions>
    <add input="{REMOTE_ADDR}" matchType="Pattern" pattern="^(?:::1|127\\.0\\.0\\.1|${listed})$" ignoreCase="true" negate="true" />
    <add input="{HTTP_X_FORWARDED_FOR}" matchType="Pattern" pattern="(?:^|,\\s*)(?:${listed})$" ignoreCase="true" negate="true" />
  </conditions>
  ${({
		// Only the answer chosen is made: the redirect's address is checked only when it is the one
		forbidden: () => '<action type="CustomResponse" statusCode="403" statusReason="Forbidden" statusDescription="Access denied" />',
		notfound: () => '<action type="CustomResponse" statusCode="404" statusReason="Not Found" statusDescription="Not found" />',
		redirect: () => `<action type="Redirect" url="${xmlEscape(redirectTargetOf(v.target, wholeSite(v)))}" redirectType="Found" />`,
	}[v.response] || (() => '<action type="AbortRequest" />'))()}
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
  <action type="Redirect" url="https://${domain}/{R:0}" appendQueryString="true" redirectType="${v.status === "temporary" ? "Found" : "Permanent"}" />
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
// Umbraco's settings for appsettings.json, from its documentation (docs.umbraco.com, Configuration): each a part of the
// file, merged in where it belongs. Nothing is set for a site by Harbor itself: they are chosen here, site by site.
const randomKey = () => btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(64))));
const appsettingsSnippets = {
	resize: { label: "Image size limits", icon: "sliders", line: "No image larger than 5000 × 5000: no huge resizes asked for in a URL",
		values: () => ({ Umbraco: { CMS: { Imaging: { Resize: { MaxWidth: 5000, MaxHeight: 5000 } } } } }) },
	imageCache: { label: "Image cache", icon: "clock", line: "Resized images kept a year, browsers keep them a week",
		values: () => ({ Umbraco: { CMS: { Imaging: { Cache: { BrowserMaxAge: "7.00:00:00", CacheMaxAge: "365.00:00:00" } } } } }) },
	imageMemory: { label: "Image memory pool", icon: "activity", line: "ImageSharp's own memory settings for resizing",
		note: "Try it on a test site first, and look at its memory after: the other Memory settings decide its limits.",
		values: () => ({ Umbraco: { CMS: { Imaging: { Memory: { Enabled: true } } } } }) },
	hmac: { label: "Image URL signing (HMAC)", icon: "key", line: "Only signed image URLs are resized: a key of the site's own",
		note: "With a key, an image URL made by hand (media.Url() + \"?width=400\") answers 400 and the image breaks. Only for a site that makes its image URLs with GetCropUrl. Try it on a test site first.",
		values: () => ({ Umbraco: { CMS: { Imaging: { HMACSecretKey: randomKey() } } } }) },
	uploads: { label: "Larger uploads", icon: "upload", line: "Media up to 200 MB in the backoffice (Umbraco's own limit is about 28 MB)",
		note: "MaxRequestLength is in kilobytes: 204800 is 200 MB (2000 would be 2 MB, lower than Umbraco's own). Apache lets up to 1 GB through, and waits 5 minutes for an upload: a larger file over a slow line may need more time.",
		values: () => ({ Umbraco: { CMS: { Runtime: { MaxRequestLength: 204800 } } } }) },
	versions: { label: "Content version cleanup", icon: "eraser", line: "Old versions of content cleaned up: all kept a week, one a day for 90 days",
		values: () => ({ Umbraco: { CMS: { Content: { ContentVersionCleanupPolicy: { EnableCleanup: true, KeepAllVersionsNewerThanDays: 7, KeepLatestVersionPerDayForDays: 90 } } } } }) },
	unattended: { label: "Unattended upgrades", icon: "upload", line: "Umbraco upgrades its database itself after a new version is published",
		values: () => ({ Umbraco: { CMS: { Unattended: { UpgradeUnattended: true } } } }) },
	production: { label: "Production runtime mode", icon: "shield", line: "Settings locked for production: no models built at runtime, no debug",
		note: "Production mode needs ModelsBuilder off (Nothing) and debug off, both set here too. The site must have its models in its code, built before it is published.",
		values: () => ({ Umbraco: { CMS: { Runtime: { Mode: "Production" }, ModelsBuilder: { ModelsMode: "Nothing" }, Hosting: { Debug: false } } } }) },
	passwords: { label: "Stronger backoffice passwords", icon: "lock", line: "Users' passwords at least 12 characters, with a symbol",
		values: () => ({ Umbraco: { CMS: { Security: { UserPassword: { RequiredLength: 12, RequireNonLetterOrDigit: true } } } } }) },
	smtp: { label: "Email (SMTP)", icon: "users", line: "How the site sends email: change the values after",
		note: "The values are examples: set your own host, port, sender and login before you save.",
		values: () => ({ Umbraco: { CMS: { Global: { Smtp: { From: "noreply@example.com", Host: "smtp.example.com", Port: 587, SecureSocketOptions: "StartTls", Username: "", Password: "" } } } } }) },
	trailingSlash: { label: "No trailing slash", icon: "chevron", line: "Umbraco writes its own links without a / at the end",
		note: "This only changes how Umbraco writes its links; it redirects nothing. With the Apache rule AddTrailingSlash in Edit config (it sends /page to /page/), every link Umbraco writes would then be redirected once: use one or the other.",
		values: () => ({ Umbraco: { CMS: { RequestHandler: { AddTrailingSlash: false } } } }) },
	deliveryApi: { label: "Content Delivery API", icon: "globe", line: "Content as JSON for a front end of its own, public",
		values: () => ({ Umbraco: { CMS: { DeliveryApi: { Enabled: true, PublicAccess: true } } } }) },
	logging: { label: "Less logging", icon: "file", line: "Only warnings and errors in the log",
		values: () => ({ Serilog: { MinimumLevel: { Default: "Warning" } } }) },
	// For any ASP.NET Core site: what the framework itself reads, not Umbraco
	dotnetLogging: { label: "Less logging", icon: "file", line: "Only warnings and errors in the log", sites: "dotnet",
		values: () => ({ Logging: { LogLevel: { Default: "Warning", "Microsoft.AspNetCore": "Warning" } } }) },
	allowedHosts: { label: "Allowed hosts", icon: "globe", line: "Only answer requests for the site's own names", sites: "any",
		note: "The names are examples: set the site's own domains, separated by ;, before you save. A name left out gets 400.",
		values: () => ({ AllowedHosts: "example.dk;www.example.dk" }) },
	dotnetUploads: { label: "Larger requests", icon: "upload", line: "Requests up to 200 MB (Kestrel's own limit is about 28 MB)", sites: "dotnet",
		note: "MaxRequestBodySize is in bytes: 209715200 is 200 MB. A form upload has a limit of its own in the code (FormOptions, 128 MB), which this does not change.",
		values: () => ({ Kestrel: { Limits: { MaxRequestBodySize: 209715200 } } }) },
};
// The snippets for the open file's site: Umbraco's for an Umbraco site, only the general ones for any other
function appsettingsChoices() {
	const umbraco = openFileSite?.kind === "umbraco";
	return Object.fromEntries(Object.entries(appsettingsSnippets).filter(([, snippet]) =>
		snippet.sites === "any" || (umbraco ? !snippet.sites : snippet.sites === "dotnet")));
}
// The paths and values a snippet sets, as lines: Umbraco:CMS:Imaging:Memory:Enabled = true
function settingLines(values, prefix = "") {
	return Object.entries(values).flatMap(([key, value]) => value && typeof value === "object" && !Array.isArray(value)
		? settingLines(value, prefix + key + ":") : [`${prefix}${key} = ${JSON.stringify(value).replace(/^"(.{24}).{20,}(.{4})"$/, '"$1…$2"')}`]);
}
// A snippet merged into the file: only its own keys set, the rest as it was. The file is written again with its own
// indentation; comments in it (allowed in appsettings.json) would go, so a file with them is left alone.
function mergeSettings(target, values) {
	for (const [key, value] of Object.entries(values)) {
		const existing = Object.keys(target).find((name) => name.toLowerCase() === key.toLowerCase()) ?? key;
		if (value && typeof value === "object" && !Array.isArray(value) && target[existing] && typeof target[existing] === "object") mergeSettings(target[existing], value);
		else target[existing] = value;
	}
	return target;
}
async function insertAppsetting(key) {
	const snippet = appsettingsChoices()[key];
	if (!snippet) return;
	const text = editorValue();
	let data;
	try {
		data = JSON.parse(text.replace(/^\uFEFF/, "") || "{}");
	} catch (error) {
		throw new Error(/\/\/|\/\*/.test(text) ? "The file has comments: add the setting by hand, as writing it again would drop them." : `The file is not valid JSON: ${error.message}`);
	}
	const values = snippet.values();
	const lines = settingLines(values);
	if (!(await ask({ title: snippet.label, message: (snippet.note ? snippet.note + "\n\n" : "") + "Sets in appsettings.json:\n" + lines.join("\n"), input: false, ok: "Insert" }))) {
		focusEditor();
		return;
	}
	const indent = (/\n([ \t]+)"/.exec(text) || [, "  "])[1];
	editorReplaceAll(JSON.stringify(mergeSettings(data, values), null, indent) + (text.endsWith("\n") ? "\n" : ""));
	toast(`${snippet.label} inserted. Check it, then save; the site uses it from its next start.`, "ok");
}

const apacheSnippets = {
	ignore: { label: "Ignore", build: (v) => `# Ignore
RewriteRule ^/(?:${pathPattern(pathsOf(v.paths))})/ - [L]` },
	ips: { label: "Allowed IPs", build: (v) => {
		const addresses = addressesOf(v.addresses, true);
		// An expression, as mod_rewrite cannot match ranges; anyone else gets 403. The addresses are joined with ||
		// under one !: in Apache's expressions ! covers everything after it, so "! a && ! b" does not mean "neither".
		return `# ${v.name || "Allowed IPs"}${wholeSite(v) ? "\n" + ACME_APACHE : ""}
RewriteCond expr "!(${["127.0.0.0/8", "::1", ...addresses].map((address) => `-R '${address}'`).join(" || ")})"
RewriteRule ${wholeSite(v) ? "^/" : `^/(?:${pathPattern(pathsOf(v.paths))})(?:/|$)`} ${({
		// Only the answer chosen is made: the redirect's address is checked only when it is the one
		notfound: () => "- [R=404,NC,L]",
		redirect: () => `${redirectTargetOf(v.target, wholeSite(v))} [R=302,NC,L]`,
	}[v.response] || (() => "- [F,NC,L]"))()}`;
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
RewriteRule ^ https://${domain}%{REQUEST_URI} [R=${v.status === "temporary" ? 302 : 301},L]`;
	} },
	// Sent straight to the site's service, written as the site's own robots.txt and sitemap.xml are in its config: a
	// ProxyPass, before ProxyPass / (a rewrite that only changed the path inside Apache, PT, never reaches the service)
	rewrite: { label: "Rewrite from → to", build: (v, config) => {
		const port = /^[ \t]*ProxyPass\s+\/\s+http:\/\/127\.0\.0\.1:(\d+)\//m.exec(config);
		if (!port) throw new Error("This config has no ProxyPass / to the site's service, so a rewrite cannot be sent there.");
		const rewrite = rewriteOf(v);
		const target = `http://127.0.0.1:${port[1]}/${rewrite.to}`;
		return `# Rewrite ${rewrite.from}
ProxyPass /${rewrite.from} ${target}
ProxyPassReverse /${rewrite.from} ${target}`;
	} },
	// Ubuntu's Apache compresses HTML, CSS and JavaScript already; this adds JSON, SVG and the rest of text for the site
	compression: { label: "Compression", place: "end", build: () => `# Compression
<IfModule mod_deflate.c>
  AddOutputFilterByType DEFLATE text/html text/plain text/css text/xml text/javascript application/javascript application/json application/xml image/svg+xml
</IfModule>` },
	cookies: { label: "Add Secure to ALL cookies", place: "end", build: () => `# Add Secure to ALL cookies
Header always edit Set-Cookie "(?i)^((?!.*;\\s*secure).*)$" "$1; Secure"` },
};

// Your own address under a list of addresses, as in Who can reach the admin site: Add it, or In the list
let yourAddress = null;
function yourAddressCard(input) {
	const value = el("strong", { textContent: "…" });
	const listed = el("span", { className: "your-address-in", hidden: true }, svgIcon("check"), "In the list");
	const add = el("button", { type: "button", className: "small primary", textContent: "+ Add it", hidden: true });
	const card = el("div", { className: "your-address" },
		el("span", { className: "your-address-icon", ariaHidden: "true" }, svgIcon("globe")),
		el("span", { className: "your-address-text" }, el("small", { textContent: "Your address now" }), value), listed, add);
	const items = () => input.value.split(/[\s,]+/).filter(Boolean);
	const show = () => {
		if (!yourAddress) return;
		const inList = items().some((item) => addressIn(yourAddress, item));
		listed.hidden = !inList;
		add.hidden = inList;
	};
	add.onclick = () => {
		if (!items().includes(yourAddress)) input.value = [...items(), yourAddress].join(", ");
		show();
		input.focus();
	};
	input.addEventListener("input", show);
	(yourAddress ? Promise.resolve({ address: yourAddress }) : api("/api/your-address"))
		.then(({ address }) => {
			if (!address) throw new Error();
			yourAddress = address;
			value.textContent = address;
			show();
		})
		.catch(() => card.remove());
	return card;
}

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
		if (field.options) {
			// A choice is segments side by side, the first chosen; a choice for one kind of config only is left out elsewhere
			const options = field.options.filter((option) => !option[3] || option[3] === kind);
			const segments = el("div", { className: "samba-segments", role: "radiogroup", ariaLabel: field.label },
				...options.map(([value, title, line], index) => el("label", { className: "samba-segment" },
					el("input", { type: "radio", name: field.name, value, checked: index === 0 }),
					el("strong", { textContent: title }), ...(line ? [el("small", { textContent: line })] : []))));
			segments.style.gridTemplateColumns = `repeat(${options.length}, minmax(0, 1fr))`;
			const row = el("fieldset", { className: "snippet-choice" }, el("legend", { textContent: field.label }), segments);
			row.dataset.field = field.name;
			return [field, row];
		}
		// Umbraco's own paths are filled in for an Umbraco site only; any other site gets an example to replace
		const value = field.umbraco && openFileSite?.kind === "umbraco" ? field.umbraco : field.value || "";
		const input = el("input", { name: field.name, value, placeholder: field.placeholder || "", autocomplete: "off", spellcheck: false });
		const hint = (openFileSite?.kind === "umbraco" && field.umbracoHint) || field.hint;
		const row = el("label", {}, field.label, input, ...(hint ? [el("small", { className: "hint", textContent: hint })] : []));
		row.dataset.field = field.name;
		return [field, row];
	});
	$("#snippet-fields").replaceChildren(...rows.flatMap(([field, row]) => field.yourAddress ? [el("div", { className: "snippet-field-group" }, row, yourAddressCard(row.querySelector("input")))] : [row]));
	// A field that belongs to one choice, like the address of a redirect, is only there when it is chosen
	const showFields = () => {
		const whole = form.elements.scope?.value === "site";
		for (const [field, row] of rows) {
			if (field.wholeSite) {
				// Its suggestion follows what is protected: / for chosen paths, an address elsewhere for the whole site
				const input = row.querySelector("input");
				const hint = row.querySelector(".hint");
				if (whole && input.value.trim() === field.value) input.value = "";
				if (!whole && !input.value.trim()) input.value = field.value;
				input.placeholder = whole ? field.wholeSite.placeholder : field.placeholder || "";
				if (hint) hint.textContent = whole ? field.wholeSite.hint : field.hint;
			}
			if (!field.showIf) continue;
			const chosen = form.elements[field.showIf[0]]?.value === field.showIf[1];
			row.hidden = !chosen;
			for (const input of row.querySelectorAll("input, select")) input.disabled = !chosen;
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

// The site a file in the editor belongs to: by the site it was opened for (site or site@target), or by the folder it is in
function siteOfFile(path, remoteSite) {
	const ref = remoteSite ?? remoteFilesSite;
	if (ref) return allSites.find((site) => site.name === ref.split("@")[0]) || null;
	// Folders under /var/www are compared as the API gives them, relative to it
	const relative = (folder) => folder.replace(/^\/var\/www\//, "").replace(/^\/+|\/+$/g, "");
	const full = relative(remoteSite === null && !serverFiles ? join(localFilesRoot, path) : path);
	const inside = (folder) => !!folder && (full + "/").startsWith(relative(folder) + "/");
	return allSites.find((site) => site.environments.some((env) => inside(env.path) || inside(env.publish_path))) || null;
}

function snippetKind() {
	const name = (openFile || "").split("/").pop().toLowerCase();
	if (name === "web.config") return "iis";
	if (/^appsettings(\.[\w-]+)?\.json$/.test(name)) return "appsettings";
	if (configurationEditor?.kind === "conf" || (/\.conf$/.test(name) && /sites-(available|enabled)/.test(openFile || ""))) return "apache";
	return "";
}

const snippetLooks = {
	ignore: ["eraser", "Leave chosen paths alone"],
	ips: ["shield", "Only listed addresses reach the site or chosen paths"],
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
	const snippets = kind === "iis" ? iisSnippets : kind === "appsettings" ? appsettingsChoices() : apacheSnippets;
	$("#editor-snippets-panel").replaceChildren(
		el("div", { className: "snippet-panel-head" }, el("span", { textContent: kind === "iis" ? "IIS · WEB.CONFIG" : kind === "appsettings" ? (openFileSite?.kind === "umbraco" ? "UMBRACO" : ".NET") + " · APPSETTINGS.JSON" : "APACHE · SITE CONFIG" }),
			el("strong", { textContent: kind === "appsettings" ? "Insert a setting" : "Insert a rule" })),
		...Object.entries(snippets).map(([key, snippet]) => {
			const [icon, line] = kind === "appsettings" ? [snippet.icon, snippet.line] : snippetLooks[key] || ["file", ""];
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
			const site = match[0].includes(`<match url="${ACME_IIS}"`);
			const addresses = /input="\{REMOTE_ADDR\}"[^>]*pattern="\^\(\?:([^)]*)\)\$"/.exec(match[0]);
			if ((!paths && !site) || !addresses) continue;
			found.push({ name: match[1], start: match.index, end: match.index + match[0].length,
				paths: site ? lowerSet(["/"]) : lowerSet(paths[1].split("|").map(unescapeRegex)),
				addresses: lowerSet(addresses[1].split("|").map(unescapeRegex).filter((item) => item !== "::1" && item !== "127.0.0.1")) });
		}
	} else {
		for (const match of text.matchAll(/^[ \t]*# (Allowed IPs[^\n]*)\n((?:[ \t]*\S[^\n]*\n?)*)/gm)) {
			const paths = /RewriteRule \^\/\(\?:([^)]*)\)/.exec(match[2]);
			const site = /RewriteRule \^\/ /.test(match[2]);
			if (!paths && !site) continue;
			found.push({ name: match[1].trim(), start: match.index, end: match.index + match[0].replace(/\n$/, "").length,
				paths: site ? lowerSet(["/"]) : lowerSet(paths[1].split("|").map(unescapeRegex)),
				addresses: lowerSet([...match[2].matchAll(/-R '([^']+)'/g)].map((item) => item[1]).filter((item) => item !== "127.0.0.0/8" && item !== "::1")) });
		}
	}
	return found;
}

// Allowed IPs for paths that already have a rule changes that rule's addresses; other paths get a rule of their own
function placeAllowedIps(kind, text, snippet, values) {
	const rules = allowedIpRules(kind, text);
	// The whole site is its own rule, apart from rules for paths
	const paths = wholeSite(values) ? lowerSet(["/"]) : lowerSet(pathsOf(values.paths));
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
	if (kind === "appsettings") return insertAppsetting(key);
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
	openFileSite = siteOfFile(path, remoteSite);
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

// Apache's own config (apache2.conf) in the editor, as a site's: Save asks to test it and reload Apache
async function editApacheConfig() {
	if (!(await discardOk())) return;
	const url = "/api/server/apache-config";
	const data = await api(url);
	configurationEditor = { url, kind: "conf", encoding: "utf-8" };
	openFileSite = null;
	openFile = data.path;
	if (!editorDialog.open) editorDialog.showModal();
	$("#editor-path").textContent = data.path;
	$("#editor-status").textContent = "";
	await showInEditor(data.path, data.content);
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
	openFileSite = site;
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

// Terminals are those to type in (a shell); actions are processes, with a window of their own
const terminalTabs = () => Object.values(terminals).filter((t) => !t.taskWindow);
const processes = () => Object.values(terminals).filter((t) => t.taskWindow);
function updateTerminalState() {
	const count = terminalTabs().length;
	$("#term-count").textContent = count;
	$("#term-count").hidden = !count;
	$("#btn-dock").hidden = !count;
	if (!count) $("#dock").hidden = true;
	updateProcesses();
}
function updateProcesses() {
	const list = processes();
	const running = list.filter((t) => !t.exited).length;
	const button = $("#btn-processes");
	button.hidden = !list.length;
	button.classList.toggle("running", running > 0);
	button.classList.toggle("problem", list.some((t) => t.taskState === "failed" || t.taskState === "warning"));
	$("#process-count").textContent = list.length;
	if (!list.length) $("#processes-menu").hidden = true;
	$("#process-list").replaceChildren(...list.map((t) => {
		const state = t.taskState || "running";
		const elapsed = Math.max(0, Math.round(((t.ended || Date.now()) - t.started) / 1000));
		const text = state === "running" ? `Running · ${elapsed < 60 ? elapsed + " s" : Math.floor(elapsed / 60) + " min"}`
			: state === "done" ? "Done" : state === "warning" ? "Finished with warnings" : "Failed";
		return el("li", {}, el("button", { type: "button", className: `process-item ${state}`, onclick: () => {
			$("#processes-menu").hidden = true;
			$("#btn-processes").setAttribute("aria-expanded", "false");
			showTaskWindow(t);
		} }, el("span", { className: "process-state", "aria-hidden": "true" }),
			el("span", { className: "process-text" }, el("span", { className: "process-title", textContent: t.taskTitle }),
				el("span", { className: "process-sub", textContent: lastReachedStep(t) && state === "running" ? lastReachedStep(t).text : text }))));
	}));
}
$("#btn-processes").onclick = (event) => {
	event.stopPropagation();
	const menu = $("#processes-menu");
	menu.hidden = !menu.hidden;
	$("#btn-processes").setAttribute("aria-expanded", String(!menu.hidden));
	if (!menu.hidden) updateProcesses();
};
document.addEventListener("click", (event) => {
	if (!$("#processes-menu").hidden && !event.target.closest(".processes")) {
		$("#processes-menu").hidden = true;
		$("#btn-processes").setAttribute("aria-expanded", "false");
	}
});
setInterval(() => { if (!$("#processes-menu").hidden) updateProcesses(); }, 1000);

function showDock() {
	$("#dock").hidden = false;
	fitActive();
}

function toggleDock() {
	if (!$("#dock").hidden) { $("#dock").hidden = true; return; }
	if (!terminalTabs().length) { toast("No terminals open. Open a shell from a site or the server."); return; }
	if (!terminals[activeTerminal] || terminals[activeTerminal].taskWindow) activate(terminalTabs()[0].id);
	showDock();
}
$("#btn-dock").onclick = toggleDock;
// Closing the window also removes the terminals whose process has finished; running ones keep going
async function closeDock() {
	for (const t of terminalTabs().filter((x) => x.exited)) await t.remove();
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

// ---- Check service: the state of a site's service in a window, its log folded out when something is wrong
const serviceDialog = $("#dlg-service");
let serviceShown = null;
const SERVICE_STATES = { running: ["ok", "Running"], activating: ["busy", "Starting"], deactivating: ["busy", "Stopping"],
	failed: ["bad", "Failed"], inactive: ["off", "Stopped"], missing: ["off", "No service"] };
function bytesText(value) {
	if (value == null) return "–";
	return value >= 1073741824 ? `${(value / 1073741824).toFixed(1)} GB` : `${Math.round(value / 1048576)} MB`;
}
function durationText(seconds) {
	if (seconds == null) return "–";
	const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
	return days ? `${days} d ${hours} h` : hours ? `${hours} h ${minutes} min` : minutes ? `${minutes} min` : `${Math.floor(seconds)} s`;
}
function serviceFact(label, value, title = "") {
	return el("div", { className: "service-fact", title }, el("span", { className: "service-fact-label", textContent: label }), el("span", { className: "service-fact-value", textContent: value }));
}
function renderService(data) {
	const key = data.loaded === "not-found" ? "missing" : data.active === "active" ? "running" : data.active;
	const [tone, text] = SERVICE_STATES[key] || ["off", data.active || "Unknown"];
	serviceDialog.dataset.tone = tone;
	$("#service-pill").textContent = text;
	$("#service-pill").className = `service-pill ${tone}`;
	$("#service-description").textContent = data.description && data.description !== `${data.unit}.service` ? data.description : data.unit;
	const since = data.since ? new Date(data.since * 1000) : null;
	const facts = [
		serviceFact(data.active === "active" ? "Running for" : "Since", since ? durationText(Date.now() / 1000 - data.since) : "–", since ? since.toLocaleString() : ""),
		serviceFact("Memory", bytesText(data.memory)),
		serviceFact("CPU time", data.cpu_seconds == null ? "–" : data.cpu_seconds < 60 ? `${data.cpu_seconds.toLocaleString(undefined, { maximumFractionDigits: 1 })} s` : durationText(data.cpu_seconds)),
		serviceFact("Port", data.port ? String(data.port) : "–"),
		serviceFact("Main PID", data.pid ? String(data.pid) : "–"),
		serviceFact("Tasks", data.tasks == null ? "–" : String(data.tasks)),
		serviceFact("Restarts", data.restarts == null ? "–" : String(data.restarts), "Times systemd restarted it after it stopped"),
		serviceFact("Starts at boot", data.enabled === "enabled" ? "Yes" : data.enabled ? "No" : "–"),
	];
	$("#service-facts").replaceChildren(...facts);
	const lines = data.log || [];
	// A line is shown from its time on: the host and unit in front of each line say nothing here
	$("#service-log").replaceChildren(...(lines.length ? lines.map((line) => {
		const match = line.match(/^(\S+)T(\d\d:\d\d:\d\d)\S*\s+\S+\s+[^:]+:\s?(.*)$/);
		const problem = /\b(errors?|fail(ed|ure)?|fatal|\w*exception|traceback|warn(ing)?s?|crit(ical)?)\b/i.test(line);
		return el("div", { className: "service-log-line" + (problem ? " problem" : "") },
			el("span", { className: "service-log-time", textContent: match ? match[2] : "" }), el("span", { textContent: match ? match[3] : line }));
	}) : [el("div", { className: "service-log-empty", textContent: "No log lines." })]));
	const trouble = key === "failed" || data.log_problem;
	setServiceLog(serviceShown.logOpen ?? trouble);
	$("#service-note").textContent = key === "failed" ? `It stopped with exit status ${data.exit_status ?? "?"}${data.result && data.result !== "success" ? ` (${data.result})` : ""}.`
		: data.log_problem ? "The latest log lines have errors or warnings." : "";
	$("#service-note").hidden = !$("#service-note").textContent;
}
function setServiceLog(open) {
	serviceShown.logOpen = open;
	$("#service-log-panel").hidden = !open;
	$("#service-log-toggle").textContent = open ? "Hide log" : "Show log";
	if (open) requestAnimationFrame(() => { $("#service-log").scrollTop = $("#service-log").scrollHeight; });
}
async function loadServiceStatus() {
	const { site, env } = serviceShown;
	serviceDialog.classList.add("loading");
	try {
		const query = isTarget(env) ? `target=${encodeURIComponent(env.target)}` : `unit=${encodeURIComponent(env.unit)}`;
		renderService(await api(`/api/sites/${encodeURIComponent(site.name)}/service?${query}`));
	} finally {
		serviceDialog.classList.remove("loading");
	}
}
async function openServiceStatus(site, env) {
	serviceShown = { site, env, logOpen: undefined };
	$("#service-title").textContent = bindingLabel(env) || env.unit || siteLabel(site);
	$("#service-pill").textContent = "Checking…";
	$("#service-pill").className = "service-pill busy";
	$("#service-description").textContent = isTarget(env) && env.mode === "remote" ? env.host || "" : "";
	$("#service-facts").replaceChildren();
	$("#service-log").replaceChildren();
	$("#service-note").hidden = true;
	setServiceLog(false);
	serviceDialog.showModal();
	await loadServiceStatus();
}
$("#service-refresh").onclick = () => run(loadServiceStatus);
$("#service-log-toggle").onclick = () => setServiceLog(!serviceShown.logOpen);
$("#service-close").onclick = () => serviceDialog.close();

// ---- An action runs in a window of its own: its steps, and the output only when something went wrong or is asked for.
// harbor commands report their steps (an OSC sequence, see harbor/core/system.py); a script shows its latest line.
const taskDialog = $("#dlg-task");
let taskShown = null;
const ANSI = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\r/g;
function taskStateIcon(state) {
	if (state === "running") return el("span", { className: "task-spinner" });
	const paths = { done: "M5 12.5l4.5 4.5L19 7.5", failed: "M7 7l10 10M17 7L7 17", warning: "M12 8v5M12 16.5v.5" };
	const svg = svgIcon("file");
	svg.querySelector("path").setAttribute("d", paths[state]);
	return svg;
}
// A command can plan its steps (harbor/core/system.py plan): those not reached yet are shown as still to come, and each
// step it reports is the next of them. A step that is not in the plan comes before those still to come.
function reachedStep(step) { return !step.pending && !step.skipped; }
function lastReachedStep(t) { return t.steps.filter(reachedStep).pop(); }
// Multi action: a warning or error in a site's output (lib/bulk_tasks.py) marks the step of that site it came in, and is
// told under it, so the sites with warnings are seen without the output. One the step already tells (a warning step) is not
// told again.
function addTaskProblem(t, event) {
	const site = String(event.site), text = String(event.text || "");
	const step = t.steps.filter((item) => item.site === site && reachedStep(item)).pop();
	if (!step || !text || step.text === text || step.problems?.includes(text)) return;
	(step.problems ||= []).push(text);
}
function addTaskStep(t, event) {
	const site = event.site ? String(event.site) : "";
	const firstPending = t.steps.findIndex((step) => step.pending && step.site === site);
	if (event.type === "plan") {
		const planned = (Array.isArray(event.steps) ? event.steps : []).map((text) => ({ type: "step", text: String(text), site, pending: true }));
		t.steps.splice(firstPending < 0 ? t.steps.length : firstPending, 0, ...planned);
		return;
	}
	const text = String(event.text || "");
	if (event.type === "skip") {
		const at = t.steps.findIndex((step) => step.pending && step.site === site && step.text === text);
		if (at >= 0) Object.assign(t.steps[at], { pending: false, skipped: true });
		return;
	}
	if (event.type === "step" && firstPending >= 0) {
		const at = t.steps.findIndex((step, index) => index >= firstPending && step.pending && step.site === site && step.text === text);
		if (at >= 0) {
			// Planned steps it went past were not needed
			for (let index = firstPending; index < at; index++) if (t.steps[index].pending && t.steps[index].site === site) Object.assign(t.steps[index], { pending: false, skipped: true });
			t.steps[at].pending = false;
			return;
		}
	}
	t.steps.splice(firstPending < 0 ? t.steps.length : firstPending, 0, { type: event.type, text, site });
}
// The step it is on in view: the list scrolls with it, also when it was scrolled away
function followTaskStep() {
	const list = $("#task-steps");
	const current = list.querySelector(".task-step.active, .task-step.failed") || [...list.querySelectorAll(".task-step:not(.pending):not(.skipped)")].pop();
	if (!current) { list.scrollTop = 0; return; }
	const below = current.offsetTop + current.offsetHeight + (current.nextElementSibling ? current.nextElementSibling.offsetHeight : 0);
	if (below > list.scrollTop + list.clientHeight) list.scrollTop = below - list.clientHeight;
	else if (current.offsetTop < list.scrollTop) list.scrollTop = current.offsetTop;
}
function renderTask(t) {
	if (taskShown !== t) return;
	const state = t.taskState || "running";
	taskDialog.dataset.state = state;
	$("#task-state").replaceChildren(taskStateIcon(state));
	$("#task-title").textContent = t.taskTitle;
	const elapsed = Math.max(0, Math.round((Date.now() - t.started) / 1000));
	const closing = state === "done" && t.closing;
	taskDialog.classList.toggle("closing", closing);
	$("#task-status").textContent = state === "running" ? `Working… ${elapsed < 60 ? elapsed + " s" : Math.floor(elapsed / 60) + " min " + (elapsed % 60) + " s"}`
		: closing ? "Done – closing" : state === "done" ? "Done" : state === "warning" ? "Finished with warnings: see the output" : `Failed${t.exitCode ? ` (exit ${t.exitCode})` : ""}: see the output`;
	// The newest step is always in view: at the bottom when the window opens and with each new step, also when the
	// list was scrolled up
	const reached = t.steps.filter(reachedStep).length;
	const follow = t.followSteps || reached !== t.stepsShown;
	t.followSteps = false;
	t.stepsShown = reached;
	// Several sites at once (Multi action): each site's newest step is active while that site runs, failed if it failed
	const lastOfSite = {};
	let lastIndex = -1;
	t.steps.forEach((step, index) => {
		if (!reachedStep(step)) return;
		if (step.site) lastOfSite[step.site] = index;
		else lastIndex = index;
	});
	$("#task-steps").replaceChildren(...t.steps.map((step, index) => {
		const last = index === lastIndex;
		const siteState = step.site && t.siteStates ? t.siteStates[step.site] : "";
		// Still to come while it runs; not taken when it has ended
		const running = state === "running" && (!step.site || !siteState || siteState === "running");
		const kind = step.skipped || (step.pending && !running) ? "skipped" : step.pending ? "pending" : step.type === "warning" ? "warning"
			: step.site && siteState ? (lastOfSite[step.site] === index && siteState === "running" && state === "running" ? "active"
				: lastOfSite[step.site] === index && siteState === "failed" ? "failed" : "done")
			: last && state === "running" ? "active" : last && state === "failed" ? "failed" : "done";
		// The site in front, unless the step names it already
		const text = step.site && !step.text.includes(step.site) ? `${step.site}: ${step.text}` : step.text;
		// A step that went on with a warning: a warning mark, not the tick, and what it said under it
		const problems = step.problems || [];
		const shown = problems.length && (kind === "done" || kind === "active") ? `${kind} warning` : kind;
		return el("li", { className: `task-step ${shown}` }, el("span", { className: "task-step-mark", "aria-hidden": "true" }),
			el("span", { className: "task-step-text" }, el("span", { textContent: text }),
				...problems.map((problem) => el("span", { className: "task-step-problem", textContent: problem }))));
	}));
	$("#task-steps").hidden = !t.steps.length;
	if (follow) followTaskStep();
	// A script without steps shows what it is doing now
	$("#task-current").textContent = state === "running" && !reached ? t.lastLine || "Starting…" : "";
	$("#task-current").hidden = !$("#task-current").textContent;
	$("#task-background").hidden = state !== "running";
	// A window that closes by itself has no Close button to miss
	$("#task-close").hidden = state === "running" || closing;
}
function setTaskOutput(t, open) {
	t.outputOpen = open;
	$("#task-details").hidden = !open;
	$("#task-toggle").textContent = open ? "Hide output" : "Show output";
	// Again once the window has its size (the terminal also follows its own size from then on)
	if (open) requestAnimationFrame(() => { try { t.fit.fit(); } catch (_) { /* not measured yet */ } requestAnimationFrame(() => { try { t.fit.fit(); } catch (_) { /* not measured yet */ } }); });
}
function showTaskWindow(t) {
	if (taskShown && taskShown !== t) hideTaskWindow();
	taskShown = t;
	t.pane.hidden = false;
	$("#task-details").replaceChildren(t.pane);
	setTaskOutput(t, !!t.outputOpen);
	t.followSteps = true;
	renderTask(t);
	if (!taskDialog.open) taskDialog.showModal();
	// Once the window has its size
	requestAnimationFrame(followTaskStep);
	clearInterval(t.clock);
	t.clock = setInterval(() => (t.taskState === "running" ? renderTask(t) : clearInterval(t.clock)), 1000);
}
// The task goes on, and is one of the terminals: its tab shows it again
function hideTaskWindow() {
	const t = taskShown;
	taskShown = null;
	if (taskDialog.open) taskDialog.close();
	// A window the task was started from opens again when its window is closed after it ended
	if (t && t.taskState && t.taskState !== "running" && t.afterTask) {
		const after = t.afterTask;
		t.afterTask = null;
		setTimeout(after, 0);
	}
	if (!t || !terminals[t.id]) return;
	t.pane.hidden = true;
	$("#panes").append(t.pane);
	updateProcesses();
}
function finishTask(t, code, problem) {
	t.taskState = code === 0 && !problem ? "done" : code === 0 ? "warning" : "failed";
	t.ended = Date.now();
	updateProcesses();
	if (t.taskState !== "done") {
		if (taskShown !== t) showTaskWindow(t);
		setTaskOutput(t, true);
	}
	renderTask(t);
	// Someone reading the output decides when it closes
	if (t.taskState === "done" && t.autoClose && !(taskShown === t && t.outputOpen)) {
		// The tick is seen for a moment, with a line running out, then it closes by itself. Show output keeps it open.
		t.closing = true;
		renderTask(t);
		t.closeTimer = setTimeout(() => {
			if (!t.closing) return;
			if (taskShown === t) hideTaskWindow();
			if (terminals[t.id]) t.remove();
		}, 1000);
	}
}
$("#task-toggle").onclick = () => {
	const t = taskShown;
	if (!t) return;
	// Looking at the output keeps a finished window open, with its Close button
	if (t.closing) {
		t.closing = false;
		clearTimeout(t.closeTimer);
		renderTask(t);
	}
	setTaskOutput(t, !t.outputOpen);
};
$("#task-background").onclick = hideTaskWindow;
$("#task-close").onclick = () => {
	const t = taskShown;
	hideTaskWindow();
	if (t && t.exited && terminals[t.id]) t.remove();
};
taskDialog.addEventListener("cancel", (event) => {
	// Escape hides a running task, and closes a finished one
	event.preventDefault();
	(taskShown?.exited ? $("#task-close") : $("#task-background")).click();
});

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
		// A process's output is in its window, never a pane of the dock
		if (t.taskWindow && taskShown === t) continue;
		t.pane.hidden = key !== id || t.taskWindow;
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
		if (message.o !== undefined) {
			t.term.write(message.o);
			// The latest line of output, for a script's task window
			const lines = message.o.replace(ANSI, "").split("\n").map((line) => line.trim()).filter(Boolean);
			if (lines.length) {
				t.lastLine = lines[lines.length - 1].slice(0, 200);
				if (taskShown === t && !lastReachedStep(t)) renderTask(t);
			}
		}
		if (message.x !== undefined) {
			t.exited = true;
			updateSiteActivity();
			t.exitCode = message.x;
			if (t.taskWindow) {
				finishTask(t, message.x, message.output_problem !== false);
				run(currentView === "sql" ? loadSqlConnections : loadSites);
				return;
			}
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

// Which terminals take keys, as the server says (describe in app.py)
const terminalInput = {};
function openTerminal(id, title, autoClose, sites = [], running = true, scope = "main", targets = []) {
	if (terminals[id]) return activate(id);
	const pane = el("div", { className: "pane", hidden: true });
	const titleNode = el("span", { textContent: title });
	const close = el("span", { className: "x", textContent: "✕", title: "Close (stops the process)" });
	const tab = el("div", { className: "tab", onclick: () => activate(id) }, titleNode, close);
	$("#tabs").append(tab);
	$("#panes").append(pane);

	// A user's tasks take no keys: the server ignores them as well, so Ctrl+C cannot leave a task for a shell. Their
	// shell in a site does: it runs as their own Linux user
	const typing = currentUser.admin || terminalInput[id] === true;
	const term = new Terminal({ cursorBlink: typing, disableStdin: !typing, fontSize: 14, scrollback: 5000, fontFamily: "ui-monospace, Menlo, Consolas, monospace", theme: { background: "#000000" } });
	const fit = new FitAddon.FitAddon();
	term.loadAddon(fit);
	term.open(pane);
	const t = { id, term, fit, pane, tab, title: titleNode, taskTitle: title, sites, scope, targets: targets || [], exited: !running, ws: null, autoClose: !!autoClose, remove: null,
		steps: [], lastLine: "", taskState: running ? "running" : "done", started: Date.now(), taskWindow: false, outputOpen: false };
	// Steps a harbor command reports: not shown in the terminal itself
	term.parser.registerOscHandler(7777, (data) => {
		if (!data.startsWith("harbor;")) return false;
		try {
			const event = JSON.parse(data.slice("harbor;".length));
			if (["step", "warning", "plan", "skip"].includes(event.type)) addTaskStep(t, event);
			if (event.type === "problem" && event.site) addTaskProblem(t, event);
			// Multi action: a site has started or ended (several run at once)
			if (event.type === "site" && event.site) (t.siteStates ||= {})[event.site] = String(event.state || "");
			renderTask(t);
		} catch (_) { /* not ours */ }
		return true;
	});
	terminals[id] = t;
	updateSiteActivity();

	// Enter or Escape closes finished terminals, including failed processes.
	term.attachCustomKeyEventHandler((event) => {
		if (t.exited && (event.key === "Enter" || event.key === "Escape")) {
			if (event.type === "keydown") closeDock();
			return false;
		}
		return true;
	});
	term.onData((data) => t.ws && t.ws.readyState === 1 && t.ws.send(JSON.stringify({ t: "i", d: data })));
	term.onResize(({ rows, cols }) => t.ws && t.ws.readyState === 1 && t.ws.send(JSON.stringify({ t: "r", rows, cols })));
	// Fitted to wherever it is shown, the dock or a task's window: its own size decides, not the dock's
	new ResizeObserver(() => { if (!pane.hidden && pane.clientWidth > 0 && pane.clientHeight > 0) { try { fit.fit(); } catch (_) { /* ignore */ } } }).observe(pane);
	setInterval(() => t.ws && t.ws.readyState === 1 && t.ws.send(JSON.stringify({ t: "p" })), 30000);

	t.remove = async () => {
		await run(() => api(`/api/term/${id}/close`, { json: {} }));
		t.exited = true;
		if (t.ws) t.ws.close();
		if (taskShown === t) hideTaskWindow();
		clearInterval(t.clock);
		term.dispose(); tab.remove(); pane.remove();
		delete terminals[id];
		updateSiteActivity();
		activeTerminal = terminalTabs()[0]?.id || null;
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
	terminalInput[data.id] = data.input;
	openTerminal(data.id, data.title, data.auto_close, data.sites, data.running, data.scope, data.targets);
	// An action gets a window of its own; a shell, or a task that asks for input, stays a terminal
	if (terminals[data.id] && data.process) {
		const t = terminals[data.id];
		markProcess(t);
		showTaskWindow(t);
		return t;
	}
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

// A process has no tab in the dock: it is under Processes, and its output in its window
function markProcess(t) {
	t.taskWindow = true;
	t.tab.hidden = true;
	t.pane.hidden = true;
	const first = terminalTabs()[0];
	activeTerminal = first ? first.id : null;
	if (first) activate(first.id);
	updateTerminalState();
}

async function restoreTerminals() {
	const { sessions } = await api("/api/term");
	for (const s of sessions) {
		terminalInput[s.id] = s.input;
		openTerminal(s.id, s.title, s.auto_close, s.sites, s.running, s.scope, s.targets);
		const t = terminals[s.id];
		if (!t || !s.process) continue;
		if (!s.running) t.taskState = s.exit === 0 && !s.output_problem ? "done" : s.exit === 0 ? "warning" : "failed";
		markProcess(t);
	}
	for (const t of Object.values(terminals)) t.pane.hidden = true;
	if (terminalTabs().length) activate(terminalTabs()[0].id);
}

run(async () => {
	syncSiteForm();
	try { currentUser = await api("/api/me"); } catch (error) { /* the page goes on as before */ }
	applyAccess();
	// The views are hidden until the address picks one, so a site's page does not show the list first. Meanwhile
	// the page says it is loading, not an empty background
	if (location.hash.startsWith("#sql")) $("#page-loading p").textContent = "Loading…";
	try { await loadSites(); } finally {
		try { await route(); } finally { $("#page-loading").hidden = true; }
	}
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
		: "The keep-alive cron job is missing. Run harbor server init on this server to install it.";
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
			showServerAddresses();
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
		$("#system-update-title").textContent = state.rollback ? `Rolling back to ${state.to}` : state.to ? `Updating to ${state.to}` : "Updating to the latest";
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
		$("#system-update-title").textContent = existing?.rollback ? "Rollback not completed" : "Update not completed";
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
	// It opens with Windows and Apache folded
	for (const fold of settingsMenu.querySelectorAll(".menu-fold")) {
		fold.setAttribute("aria-expanded", "false");
		$("#" + fold.getAttribute("aria-controls")).hidden = true;
	}
	// Windows is for the few who have it: Copy sites when a site here has an environment on Windows, Import sites when
	// a Windows server is kept in Servers, and Windows itself when either is there
	showWindowsMenu();
	run(async () => { windowsServerCount = (await api("/api/windows/imported")).servers.length; showWindowsMenu(); });
	if (!settingsMenu.hidden) run(() => checkSystemUpdate(true));
};
document.addEventListener("click", () => { settingsMenu.hidden = true; });
$("#settings-restart-apache").onclick = () => run(async () => {
	if (await ask({ title: "Restart Apache?", message: "All sites on this server are briefly unavailable.", input: false, ok: "Restart", danger: true })) await openTask({ kind: "apache", tool: "restart" });
});
let windowsServerCount = 0;
function showWindowsMenu() {
	const copy = allSites.some((site) => (site.environments || []).some((env) => env.mode === "windows"));
	$("#settings-windows-move").hidden = !copy;
	$("#settings-windows-import").hidden = !windowsServerCount;
	$("#settings-windows-fold").hidden = !copy && !windowsServerCount;
	if ($("#settings-windows-fold").hidden) $("#settings-windows-sub").hidden = true;
}
$("#settings-admin-site").onclick = () => { const admin = allSites.find((s) => s.admin); if (admin) go(siteLink(admin.name)); };
// Windows and Apache fold out in the menu, with their items under them: the menu stays short when it opens
for (const fold of settingsMenu.querySelectorAll(".menu-fold")) {
	// Its icon as the other items' (decorate): the same size, place and colour
	fold.dataset.tone = fold.dataset.foldTone;
	fold.prepend(svgIcon(fold.dataset.icon));
	fold.append(el("span", { className: "menu-fold-chevron" }, svgIcon("chevron")));
	fold.onclick = (event) => {
		event.stopPropagation();
		const sub = $("#" + fold.getAttribute("aria-controls"));
		sub.hidden = !sub.hidden;
		fold.setAttribute("aria-expanded", String(!sub.hidden));
	};
}
$("#settings-apache-test").onclick = () => run(() => openTask({ kind: "apache", tool: "configtest" }));
$("#settings-apache-conf").onclick = () => run(editApacheConfig);
// Whether an address is a listed address, or in a listed IPv4 range (a.b.c.d/n); an IPv6 address only as it is listed
function addressIn(address, item) {
	if (address === item) return true;
	const [base, bits] = item.split("/");
	const v4 = (text) => /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.test(text) ? text.split(".").reduce((sum, part) => sum * 256 + Number(part), 0) : null;
	const [mine, start] = [v4(address), v4(base)];
	if (mine === null || start === null || bits === undefined) return false;
	const size = 2 ** (32 - Math.min(32, Math.max(0, Number(bits))));
	return Math.floor(mine / size) === Math.floor(start / size);
}
// Who can reach the admin site: everywhere, or some addresses (its Apache config), from a button on its page.
// Leaving out your own address locks you out at once, so that is said and must be ticked.
const openAdminAccess = () => run(async () => {
	const dialog = $("#dlg-admin-access");
	const { addresses, yours, response = "forbidden", target = "" } = await api("/api/admin-access");
	const list = $("#admin-access-list");
	const mode = (value) => { for (const radio of dialog.querySelectorAll("[name=access_mode]")) radio.checked = radio.value === value; };
	const lines = () => list.value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
	const update = () => {
		const some = dialog.querySelector("[name=access_mode]:checked")?.value === "some";
		$("#admin-access-list-row").hidden = !some;
		list.required = some;
		// The address to send others to, only when they are sent somewhere
		const redirect = responseChoice() === "redirect";
		$("#admin-access-target-row").hidden = !redirect;
		$("#admin-access-target").required = some && redirect;
		$("#admin-access-lockout-row").hidden = true;
		$("#admin-access-lockout").checked = false;
	};
	list.value = addresses.join("\n");
	// What others get: three segments, the redirect's address under them when it is chosen
	const responses = [...dialog.querySelectorAll("[name=admin_access_response]")];
	const responseChoice = () => responses.find((radio) => radio.checked)?.value || "forbidden";
	for (const radio of responses) {
		radio.checked = radio.value === response;
		radio.onchange = () => { update(); if (radio.value === "redirect") $("#admin-access-target").focus(); };
	}
	$("#admin-access-target").value = target;
	mode(addresses.length ? "some" : "all");
	$("#admin-access-yours").textContent = yours;
	// Whether your address is in the list, also within a range: Add it, or In the list
	const showMine = () => {
		const listed = lines().some((item) => addressIn(yours, item));
		$("#admin-access-mine-in").hidden = !listed;
		$("#admin-access-add-mine").hidden = listed;
	};
	$("#admin-access-add-mine").onclick = () => { if (!lines().includes(yours)) list.value = [...lines(), yours].join("\n"); showMine(); list.focus(); };
	for (const radio of dialog.querySelectorAll("[name=access_mode]")) radio.onchange = update;
	list.oninput = () => { $("#admin-access-lockout-row").hidden = true; showMine(); };
	update();
	showMine();
	$("#admin-access-cancel").onclick = () => dialog.close();
	$("#form-admin-access").onsubmit = (event) => {
		event.preventDefault();
		const some = dialog.querySelector("[name=access_mode]:checked")?.value === "some";
		const answer = responseChoice();
		const payload = { addresses: some ? lines() : [], response: answer, target: answer === "redirect" ? $("#admin-access-target").value.trim() : "",
			confirm_lockout: $("#admin-access-lockout").checked };
		run(async () => {
			try {
				await api("/api/admin-access", { json: payload });
			} catch (error) {
				// Not saved: the list leaves out your address. Saying so, and asking for the tick.
				if (/locked out/.test(error.message)) { $("#admin-access-lockout-row").hidden = false; throw error; }
				throw error;
			}
			dialog.close();
			const others = { forbidden: "others get 403", notfound: "others get 404", redirect: `others are sent to ${payload.target}` }[answer];
			toast(some ? `The admin site can be reached from ${payload.addresses.length} address${payload.addresses.length === 1 ? "" : "es"} only: ${others}.` : "The admin site can be reached from everywhere.", "ok");
		});
	};
	dialog.showModal();
});
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
// This server back to the release installed before the one in use: only from its card in Servers, never from a menu
// where it is clicked by mistake
async function rollbackHarbor(earlier) {
	if (!earlier) return;
	const installed = $("#settings-version").textContent;
	if (!(await ask({
		title: `Roll back to v${earlier.version}?`,
		message: `Harbor goes back to the release installed before ${installed || "this one"} (${earlier.name}), and the admin starts again on it. The sites keep running. A later update brings the newest release back.`,
		input: false, ok: "Roll back",
	}))) return;
	if (serversDialog.open) serversDialog.close();
	await runSystemUpdate(await api("/api/system-update/run", { json: { action: "rollback" } }));
}
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
		el("span", { className: "user-text" }, el("strong", { textContent: user.name || user.username }),
			el("small", { textContent: (user.name ? user.username + " · " : "") + userSummary(user) + (user.linux ? ` · Linux user ${user.linux}` : "") })),
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
		const chip = el("label", { className: "grant-chip action" }, box, el("span", { textContent: text }));
		if (value === "terminal") chip.title = "A shell in the site's folder as their own Linux user: everything that user may do there, Files and deleting too";
		return chip;
	});
	// A terminal reaches the folder as the user's Linux user: it gives Files as well
	const actionBox = (value) => actionBoxes.map((chip) => chip.querySelector("input")).find((box) => box.value === value);
	const terminalBox = actionBox("terminal"), filesBox = actionBox("files");
	const terminalGivesFiles = () => {
		if (!terminalBox || !filesBox) return;
		if (terminalBox.checked) filesBox.checked = true;
		filesBox.disabled = terminalBox.checked;
		filesBox.closest("label").title = terminalBox.checked ? "Given by Terminal" : "";
	};
	if (terminalBox) terminalBox.addEventListener("change", terminalGivesFiles);
	terminalGivesFiles();
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
		el("div", { className: "grant-chips" }, ...envBoxes.map(([, , chip]) => chip),
			...(envKeys.length > 1 ? [el("label", { className: "grant-chip later", title: "Environments added to the site later are given too" }, newEnvs, el("span", { textContent: "Environments added later" }))] : [])),
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
	$("#user-title").textContent = user ? user.name || user.username : "Add user";
	$("#user-description").textContent = user ? "Change what they may do. It counts at once; a new password logs them out." : "They log in with this name and password.";
	$("#user-error").textContent = "";
	userForm.username.value = user?.username || "";
	userForm.elements.name.value = user?.name || "";
	userForm.password.required = !user;
	userForm.password.placeholder = user ? "Leave empty to keep it" : "At least 12 characters";
	userForm.querySelector(`input[name=role][value=${user?.role || "user"}]`).checked = true;
	userForm.disabled.checked = Boolean(user?.disabled);
	userForm.web.checked = user ? Boolean(user.web) : true;
	userForm.create_sites.checked = Boolean(user?.create_sites);
	userForm.sftp.checked = Boolean(user?.sftp);
	userForm.sftp_password.placeholder = user?.sftp ? "Leave empty to keep it" : "At least 12 characters";
	userForm.sftp_keys.dataset.loaded = "";
	$("#user-sftp-address").textContent = user?.linux ? `sftp://${user.linux}@${location.hostname}` : "made when you save";
	if (user?.sftp) {
		api(`/api/users/${encodeURIComponent(user.id)}/sftp`).then((data) => {
			userForm.sftp_keys.value = data.keys || "";
			userForm.sftp_keys.dataset.loaded = data.keys || "";
			if (data.linux) $("#user-sftp-address").textContent = `sftp://${data.linux}@${data.host}` + (data.port && data.port !== 22 ? `:${data.port}` : "");
		}).catch(() => {});
	}
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
	$("#user-sftp").hidden = !userForm.sftp.checked;
}
userForm.onchange = (event) => { if (["role", "web", "sql", "sftp"].includes(event.target.name)) showUserScope(); };
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
		const body = { username: userForm.username.value.trim(), name: userForm.elements.name.value.trim(), role: userForm.role.value, disabled: userForm.disabled.checked,
			web: userForm.web.checked, create_sites: userForm.create_sites.checked, sql: userForm.sql.checked,
			manage_connections: userForm.manage_connections.checked, sites, connections, sftp: userForm.sftp.checked };
		if (userForm.password.value) body.password = userForm.password.value;
		const sftp = {};
		if (userForm.sftp.checked && userForm.sftp_password.value) sftp.password = userForm.sftp_password.value;
		if (userForm.sftp.checked && userForm.sftp_keys.value.trim() !== (userForm.sftp_keys.dataset.loaded || "").trim()) sftp.keys = userForm.sftp_keys.value;
		let saved;
		try {
			saved = await api(id ? "/api/users/" + encodeURIComponent(id) : "/api/users", { method: id ? "PUT" : "POST", json: body });
			// The SFTP password and keys belong to their Linux user: set after it is there
			if (Object.keys(sftp).length) await api(`/api/users/${encodeURIComponent(saved.user.id)}/sftp`, { json: sftp });
		} catch (error) {
			$("#user-error").textContent = error.message;
			if (saved && !id) userForm.dataset.id = saved.user.id;
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


// ------------------------------------------------------------------ server tools (harbor server status)
// A card per part of the server. Each action runs as a task with its steps; the window opens again when it is done.
const serverToolsDialog = $("#dlg-server-tools");
const serverActionDialog = $("#dlg-server-action");
const DOTNET_CHANNELS = ["11.0", "10.0", "9.0", "8.0"];
const NODE_MAJORS = ["24", "22", "20"];
const SQL_EDITIONS = [
	["Developer", "Every feature, free, not for production"],
	["Express", "Free for production, 10 GB per database"],
	["Standard", "Licensed: production workloads"],
	["Enterprise", "Licensed: every feature"],
];

// Where Server tools was scrolled to when an action took it away: it comes back there after the action, as it does
// when it is drawn again in place (Refresh, a closed dialog over it). Opened from the menu it starts at the top.
let serverToolsScroll = null;
async function openServerTools() {
	const keep = serverToolsDialog.open ? serverToolsDialog.scrollTop : serverToolsScroll;
	serverToolsScroll = null;
	if (!serverToolsDialog.open) serverToolsDialog.showModal();
	const list = $("#server-tools-list");
	list.setAttribute("aria-busy", "true");
	if (!list.childElementCount) list.replaceChildren(loadingState("Looking at the server…"));
	try {
		const status = await api("/api/server/tools");
		$("#server-tools-os").textContent = status.os.name ? `${status.os.name} · what this server has for its sites` : "What this server has for its sites";
		list.replaceChildren(...serverToolCards(status));
		if (keep) serverToolsDialog.scrollTop = keep;
	} catch (error) {
		list.replaceChildren(el("div", { className: "server-tools-loading", textContent: error.message }));
	} finally {
		list.removeAttribute("aria-busy");
	}
}
$("#server-tools-close").onclick = () => serverToolsDialog.close();
$("#server-tools-refresh").onclick = () => openServerTools();

// Waiting for a server: a ring that turns, and what is being done under it
function loadingState(text, tag = "div") {
	return el(tag, { className: "server-tools-loading loading-state", role: "status" },
		el("span", { className: "loading-ring", ariaHidden: "true" }), el("span", { textContent: text }));
}

function toolButton(text, onclick, className = "") {
	return el("button", { type: "button", textContent: text, className, onclick: () => run(onclick) });
}

// Update where there is one to have: "Update to <version>", green; "Up to date" when there is none; the plain Update
// button when Harbor cannot tell (no answer from apt or Microsoft's list)
function updateButton(newer, onclick) {
	if (newer === false || newer === "") return el("span", { className: "tool-uptodate", textContent: "Up to date" }, svgIcon("check"));
	return toolButton(newer ? `Update to ${newer}` : "Update", onclick, (newer ? "primary " : "ghost ") + "tool-row-action");
}

function toolCard({ icon, tone, title, detail, state, stateClass, text, rows, actions }) {
	const head = el("div", { className: "tool-head" },
		el("span", { className: "tool-icon" }, svgIcon(icon)),
		el("div", { className: "tool-title" }, el("strong", { textContent: title }), el("small", { textContent: detail || "—", title: detail || "" })),
		el("span", { className: "tool-state " + (stateClass || ""), textContent: state }));
	const card = el("section", { className: "tool-card tone-" + tone }, head);
	if (text) card.append(el("p", { className: "tool-text", textContent: text }));
	if (rows && rows.length) card.append(el("ul", { className: "tool-rows" }, ...rows));
	if (actions && actions.length) card.append(el("div", { className: "tool-actions" }, ...actions));
	return card;
}

function toolRow(name, detail, { on = null, action = null, code = false } = {}) {
	const row = el("li", { className: "tool-row" });
	if (on !== null) row.append(el("span", { className: "dot" + (on ? " on" : "") }));
	row.append(el("span", { className: "tool-row-name", textContent: name }));
	row.append(code ? el("code", { textContent: detail, title: detail }) : el("span", { className: "tool-row-detail", textContent: detail, title: detail }));
	if (action) row.append(action);
	return row;
}

function serverToolCards(status) {
	const sql = status.sql_server, tools = status.sql_tools, sa = status.sa_password, cron = status.cron;
	const cards = [];
	// SQL Server, its tools and its sa password: one group, a row each with its own buttons
	const rowActions = (...buttons) => el("span", { className: "tool-row-actions" }, ...buttons);
	const sqlCard = toolCard({
		icon: "database", tone: "purple", title: "SQL Server", detail: sql.installed ? "Version " + sql.version : "For the sites' databases, when they use one here",
		state: sql.installed ? (sql.running ? "Running" : "Stopped") : "Not installed", stateClass: sql.installed ? (sql.running ? "ok" : "bad") : "",
		rows: [
			toolRow("SQL Server", sql.installed ? `Version ${sql.version}` + (sql.running ? "" : " · stopped") : "Not installed: not needed when the sites use a SQL Server elsewhere", {
				on: sql.installed && sql.running,
				action: rowActions(...(sql.installed
					? [updateButton(sql.newer ?? null, () => confirmServerTool("Update SQL Server?", `Takes the newest update of this version${sql.newer ? ` (${sql.newer})` : ""}. The service restarts, so take a backup first.`, "Update", { tool: "sql_update" })),
						toolButton("Remove…", () => askServerTool("sql_uninstall"), "ghost tool-row-action tool-danger")]
					: [toolButton("Install…", () => askServerTool("sql_install", { older: sql.packages_for, ubuntu: status.os.version }), "primary tool-row-action")])),
			}),
			toolRow("SQL tools", tools.installed ? "sqlcmd " + (tools.version || "") : "sqlcmd: runs the backups and database scripts, and SQL connections here", {
				on: tools.installed,
				action: rowActions(tools.installed
					? toolButton("Remove", () => confirmServerTool("Remove the SQL tools?", "sqlcmd goes, and the backups stop. SQL Server and its databases stay.", "Remove", { tool: "tools_uninstall" }, true), "ghost tool-row-action tool-danger")
					: toolButton("Install", () => runServerTool({ tool: "tools_install" }), "primary tool-row-action")),
			}),
			toolRow("sa password", sa.set ? "Set: the backups and database scripts log in with it" : "Not set: the backups and database scripts need it", {
				on: sa.set,
				action: rowActions(toolButton(sa.set ? "Change…" : "Set…", () => askServerTool("sa_password"), (sa.set ? "ghost" : "primary") + " tool-row-action")),
			}),
		],
	});
	cards.push(sqlCard);
	const jobs = cron.jobs.backup_everything && cron.jobs.shrink_logs;
	const someJobs = cron.jobs.backup_everything || cron.jobs.shrink_logs;
	// The backups run sqlcmd against the SQL Server here: without both there is nothing for them to do
	const canBackUp = sql.installed && tools.installed;
	const turnOn = toolButton("Turn on", () => runServerTool({ tool: "cron_on" }), "primary");
	turnOn.disabled = !canBackUp;
	const backups = toolCard({
		icon: "clock", tone: "teal", title: "Backups", detail: "Every database here, on a schedule",
		state: !canBackUp ? (someJobs ? "Cannot run" : "Not available") : jobs ? "On" : someJobs ? "Partly on" : "Off",
		stateClass: !canBackUp ? (someJobs ? "bad" : "") : jobs ? "ok" : someJobs ? "warn" : "",
		text: canBackUp ? "" : someJobs ? "Its jobs are in cron, but SQL Server or the SQL tools are not here: turn them off."
			: `Needs ${sql.installed ? "" : "SQL Server"}${sql.installed || tools.installed ? "" : " and "}${tools.installed ? "" : "the SQL tools"} on this server.`,
		rows: [
			toolRow("Backup", "Every database, every 4 hours at :15", { on: cron.jobs.backup_everything }),
			toolRow("Log shrink", "Every log, every 4 hours", { on: cron.jobs.shrink_logs }),
		],
		actions: [
			someJobs
				? toolButton("Turn off", () => confirmServerTool("Turn off the backups?", "The backup and log shrink jobs leave root's crontab. Other jobs stay.", "Turn off", { tool: "cron_off" }, true))
				: turnOn,
		],
	});
	// Not available until SQL Server and the SQL tools are both here
	backups.classList.toggle("unavailable", !canBackUp);
	cards.push(backups);
	const scheduled = cron.others.filter((line) => !/^\s*[A-Za-z_][A-Za-z0-9_]*\s*=/.test(line)).length
		+ (cron.jobs.backup_everything ? 1 : 0) + (cron.jobs.shrink_logs ? 1 : 0);
	cards.push(toolCard({
		icon: "clock", tone: "blue", title: "Scheduled jobs", detail: "root's crontab",
		state: `${scheduled} job${scheduled === 1 ? "" : "s"}`, stateClass: scheduled ? "ok" : "",
		text: "What this server runs on a schedule: the backups, certificate renewals and jobs of your own. Add, change or delete them.",
		actions: [toolButton("Open…", openCrontab, "primary")],
	}));
	const installed = new Set(status.dotnet.map((item) => item.channel));
	const missing = DOTNET_CHANNELS.filter((channel) => !installed.has(channel));
	cards.push(toolCard({
		icon: "dotnet", tone: "pink", title: ".NET", detail: status.dotnet.length ? status.dotnet.map((item) => item.channel).join(" · ") : "Not installed",
		state: status.dotnet.length ? `${status.dotnet.length} version${status.dotnet.length === 1 ? "" : "s"}` : "Not installed", stateClass: status.dotnet.length ? "ok" : "warn",
		rows: status.dotnet.flatMap((item) => {
			const sites = item.sites || [];
			const list = toolUsage(sites);
			return [toolRow(".NET " + item.channel, `SDK ${item.sdk || "—"} · runtime ${item.runtime || "—"}`, {
			action: el("span", { className: "tool-row-actions" }, usageToggle(sites, list),
				updateButton(item.update === null || item.update === undefined ? null : item.update ? (item.latest_runtime || item.latest_sdk) : false,
					() => confirmServerTool(`Update .NET ${item.channel}?`, `Installs its newest SDK${item.latest_sdk ? ` (${item.latest_sdk})` : ""} and ASP.NET Core runtime${item.latest_runtime ? ` (${item.latest_runtime})` : ""}. Sites use the new runtime when they restart.`, "Update", { tool: "dotnet_update", channel: item.channel })),
				// Any version can go but one a site uses, or the only one: the button says why, rather than the task failing.
				// The newest host stays when its version goes, so the others keep running.
				Object.assign(toolButton("Remove", () => confirmServerTool(`Remove .NET ${item.channel}?`,
					"Its SDKs and runtimes go from this server. The newest host stays, so the other versions keep running.", "Remove", { tool: "dotnet_remove", channel: item.channel }, true), "ghost tool-row-action tool-danger"),
					sites.length ? { disabled: true, title: `Used by ${sites.map((site) => site.site).join(", ")}: move them to another version first` }
						: status.dotnet.length === 1 ? { disabled: true, title: "The only version here: it stays" } : {})),
		}), el("li", { className: "tool-usage-item" }, list)];
		}),
		actions: missing.length ? [toolButton("Add version…", () => askServerTool("dotnet_install", { channels: missing }))] : [],
	}));
	const node = status.node;
	const nodeSites = node.sites || [];
	const nodeList = toolUsage(nodeSites, { major: node.version ? node.version.split(".")[0] : "" });
	const nodeCard = toolCard({
		icon: "hexagon", tone: "green", title: "Node.js", detail: node.version ? `Version ${node.version}` + (node.npm ? ` · npm ${node.npm}` : "") : "Not installed",
		state: node.version ? "Installed" : "Not installed", stateClass: node.version ? "ok" : "",
		text: node.version ? "" : "For sites that build their front end with npm. From NodeSource's repository.",
		actions: node.version
			? [usageToggle(nodeSites, nodeList),
				...(node.newer ? [toolButton(`Update to ${node.newer.split(/[-+~]/)[0]}`, () => confirmServerTool("Update Node.js?", `Its newest ${node.version.split(".")[0]} (${node.newer}). Build the sites that use it after, and restart a service that runs node.`, "Update", { tool: "node_install", major: node.version.split(".")[0] }), "primary")] : []),
				toolButton("Change version…", () => askServerTool("node_install", { current: node.version.split(".")[0], sites: nodeSites })),
				toolButton("Remove", () => confirmServerTool("Remove Node.js?", "node and npm go from this server."
					+ (nodeSites.length ? ` ${nodeSites.map((site) => site.site).join(", ")} use${nodeSites.length === 1 ? "s" : ""} it: their builds and services that need node stop working.` : ""),
					"Remove", { tool: "node_remove" }, true), "tool-danger")]
			: [toolButton("Install…", () => askServerTool("node_install", { sites: nodeSites }), "primary")],
	});
	if (node.version) nodeCard.append(nodeList);
	cards.push(nodeCard);
	// SSH: how root may log in. Harbor reaches other servers as root with a key, so a key only keeps that working.
	const sshStatus = status.ssh || {};
	const rootLogin = { yes: "Password or key", "prohibit-password": "Key only", no: "Off" }[sshStatus.root_login] || "Unknown";
	cards.push(toolCard({
		icon: "terminal", tone: "teal", title: "SSH", detail: `Root login: ${rootLogin}`,
		state: sshStatus.running ? "Running" : "Stopped", stateClass: sshStatus.running ? "ok" : "bad",
		text: sshStatus.root_login === "yes" ? "Root may log in with a password as well as a key. A key only is safer, and Harbor's own connections to its servers keep working with it." : "",
		actions: [toolButton("Root login…", () => askServerTool("ssh_root", { current: sshStatus.root_login }))],
	}));
	// Samba: shares of the server's folders, the whole server as root among them. Turned on here, not asked when the
	// server is set up.
	const smb = status.samba || {};
	const smbShares = smb.shares || [];
	const shareText = (share) => [share.path || "—", share.read_only ? "read only" : "read and write",
		share.force_user ? `as ${share.force_user}` : "", share.guest_ok ? "no login" : "", share.browseable ? "" : "hidden"].filter(Boolean).join(" · ");
	const smbUsers = smb.users || [];
	const sambaCard = toolCard({
		icon: "server", tone: "amber", title: "Samba", detail: smb.installed ? `\\\\${smb.host || location.hostname} · user root` : "Share the server's files",
		state: !smb.installed ? "Not installed" : !smbShares.length ? "No shares" : smb.running ? `${smbShares.length} share${smbShares.length === 1 ? "" : "s"}` : "Off",
		stateClass: smb.installed && smbShares.length ? (smb.running ? "ok" : "bad") : "",
		text: !smb.installed
			? "Shares the whole server (/) as root over Samba, to work on its files from Finder or Explorer. Anyone with root's Samba password can change anything on it."
			: !smb.password ? "Root has no Samba password yet: give it one to reach the shares." : "",
		rows: smbShares.map((share) => toolRow(share.name, shareText(share), {
			action: el("span", { className: "tool-row-actions" },
				toolButton("Edit…", () => askServerTool("samba_save", { share, users: smbUsers }), "ghost tool-row-action"),
				toolButton("Delete", () => confirmServerTool(`Delete the share ${share.name}?`, `${share.path} is not shared as ${share.name} any more. The folder and its files stay.`,
					"Delete", { tool: "samba_delete", name: share.name }, true), "ghost tool-row-action tool-danger")),
		})),
		actions: !smb.installed
			? [toolButton("Share the server…", () => askServerTool("samba_on", { hasPassword: smb.password }), "primary")]
			: [toolButton("Add share…", () => askServerTool("samba_save", { users: smbUsers })),
				toolButton("Add user…", () => askServerTool("samba_add_user")),
				smb.running
					? toolButton("Turn off", () => confirmServerTool("Turn Samba off?", "Its shares cannot be reached until it is turned on again. They stay as they are.", "Turn off", { tool: "samba_stop" }, true), "tool-danger")
					: toolButton("Turn on", () => runServerTool({ tool: "samba_start" }), "primary")],
	});
	// The logins: root, and users made for Samba alone (no shell, no home). Each share says who may log in to it.
	if (smb.installed && smbUsers.length) {
		sambaCard.append(el("h4", { className: "tool-subhead", textContent: "Users" }), el("ul", { className: "tool-rows" }, ...smbUsers.map((user) => toolRow(user.name,
			user.name === "root" ? "Every share it is let into, as root" : user.samba_only ? "For Samba alone: no shell, no home" : "Also a login on this server", {
				action: el("span", { className: "tool-row-actions" },
					toolButton("New password…", () => askServerTool(user.name === "root" ? "samba_password" : "samba_user_password", { name: user.name }), "ghost tool-row-action"),
					...(user.name === "root" ? [] : [toolButton("Delete", () => confirmServerTool(`Delete the Samba user ${user.name}?`,
						user.samba_only ? "Its Samba login and the Linux user made for it go. The files it wrote stay." : "Its Samba login goes. Its Linux user stays: it was there before.",
						"Delete", { tool: "samba_delete_user", name: user.name }, true), "ghost tool-row-action tool-danger")])),
			}))));
	}
	cards.push(sambaCard);
	// One column of groups: each group's buttons beside its state, its rows the full width
	for (const card of cards) {
		const actions = card.querySelector(":scope > .tool-actions");
		if (actions) card.querySelector(".tool-head").append(actions);
	}
	return cards;
}

async function runServerTool(payload) {
	serverToolsScroll = serverToolsDialog.scrollTop;
	serverToolsDialog.close();
	const t = await openTask({ kind: "server_tool", ...payload });
	if (t) t.afterTask = openServerTools;
}

async function confirmServerTool(title, message, ok, payload, danger = false) {
	if (await ask({ title, message, input: false, ok, danger })) await runServerTool(payload);
}

// A password and its repeat, side by side
function sambaPasswordFields(required, label = "Samba password") {
	return [el("div", { className: "ns-grid" },
		el("label", {}, label, el("input", { name: "samba_password", type: "password", required, minLength: 8, maxLength: 128, autocomplete: "new-password" })),
		el("label", {}, "Repeat password", el("input", { name: "samba_repeat", type: "password", required, minLength: 8, maxLength: 128, autocomplete: "new-password" })))];
}

function passwordFields() {
	return [el("div", { className: "ns-grid" },
		el("label", {}, "sa password", el("input", { name: "sa_password", type: "password", required: true, minLength: 8, maxLength: 128, autocomplete: "new-password" })),
		el("label", {}, "Repeat password", el("input", { name: "sa_repeat", type: "password", required: true, minLength: 8, maxLength: 128, autocomplete: "new-password" })))];
}

// Whether a package.json's engines.node allows a Node.js major version: true, false, or null when it cannot be told.
// Its common forms: 20, 20.x, ^20.1, ~20, >=18, >=18 <23, 18 - 22, and any of those joined by ||.
function nodeEnginesAllow(range, major) {
	if (!range || !range.trim() || range.trim() === "*") return null;
	const wanted = Number(major);
	const majorOf = (text) => { const found = /^v?(\d+)/.exec(text.trim()); return found ? Number(found[1]) : null; };
	let understood = false;
	const allows = range.split("||").some((part) => {
		part = part.trim();
		const span = /^(\S+)\s+-\s+(\S+)$/.exec(part);
		if (span) {
			const [low, high] = [majorOf(span[1]), majorOf(span[2])];
			if (low === null || high === null) return false;
			understood = true;
			return wanted >= low && wanted <= high;
		}
		const tests = part.split(/\s+/).filter(Boolean).map((piece) => {
			const found = /^(>=|<=|>|<|=|\^|~)?\s*v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?$/.exec(piece);
			if (!found) return null;
			const [operator = "", number] = [found[1], Number(found[2])];
			const exact = found[3] && /\d/.test(found[3]) && Number(found[3]) > 0 || found[4] && /\d/.test(found[4]) && Number(found[4]) > 0;
			return { ">=": wanted >= number, ">": exact ? wanted >= number : wanted > number, "<": exact ? wanted <= number : wanted < number,
				"<=": wanted <= number }[operator] ?? wanted === number;
		});
		if (!tests.length || tests.includes(null)) return false;
		understood = true;
		return tests.every(Boolean);
	});
	return understood ? allows : null;
}

// What uses a tool: a list of sites, each with how (and for Node.js, whether the versions it wants include major)
function toolUsage(sites, { major = "", installed = "" } = {}) {
	const how = { project: "Its project builds on it", published: "What is published runs on it", "package.json": "Has a package.json", service: "Its service runs node" };
	return el("ul", { className: "tool-usage" }, ...sites.map((item) => {
		const fits = major ? nodeEnginesAllow(item.engines, major) : null;
		const note = item.engines
			? `Wants Node.js ${item.engines}`
			: item.how === "package.json" || item.how === "service" ? "Says no version" : "";
		return el("li", { className: "tool-usage-row" + (fits === false ? " misfit" : "") },
			el("strong", { textContent: item.site }),
			el("span", { className: "muted", textContent: [how[item.how] || item.how, item.path && item.how === "package.json" ? item.path : "", note].filter(Boolean).join(" · ") }),
			...(major ? [el("span", { className: "tool-usage-fit " + (fits === false ? "bad" : fits ? "ok" : ""),
				textContent: fits === false ? `Not ${major}` : fits ? `${major} fits` : installed && installed === major ? "Now" : "Test it" })] : []));
	}));
}

// "Used by 3 sites": a button that unfolds the list under what it belongs to
function usageToggle(sites, list) {
	const count = sites.length;
	const button = el("button", { type: "button", className: "ghost tool-row-action tool-usage-toggle", textContent: count ? `Used by ${count}` : "Not used",
		title: count ? "Show the sites that use it" : "No site here uses it", disabled: !count, ariaExpanded: "false",
		onclick: () => { list.hidden = !list.hidden; button.setAttribute("aria-expanded", String(!list.hidden)); } });
	if (count) button.append(svgIcon("chevron"));
	list.hidden = true;
	return button;
}

function choiceCards(name, legend, options, checked) {
	return el("fieldset", { className: "server-editions" }, el("legend", { textContent: legend }),
		...options.map(([value, sub]) => el("label", { className: "type-card" },
			el("input", { type: "radio", name, value, required: true, checked: value === checked }),
			el("span", { className: "tc-title", textContent: value }),
			el("span", { className: "tc-sub", textContent: sub }))));
}

// An option as a card: its icon, what it does and what it means; chosen, it takes its colour (red for what deletes)
function checkOption(name, title, sub, checked, icon = "activity", tone = "blue") {
	return el("label", { className: "sync-option action-option tone-" + tone },
		el("input", { type: "checkbox", name, checked }),
		el("span", { className: "sync-option-icon" }, svgIcon(icon)),
		el("span", { className: "sync-option-text" }, el("span", { className: "sync-option-title", textContent: title }),
			el("span", { className: "sync-option-sub", textContent: sub })));
}

// ---- Scheduled jobs: root's crontab, a job at a time
const crontabDialog = $("#dlg-crontab");
let crontabEntries = [];
const CRON_PRESETS = [["Every hour", "0 * * * *"], ["Every 4 hours", "0 */4 * * *"], ["Every day at 03:00", "0 3 * * *"],
	["Every Sunday at 03:00", "0 3 * * 0"], ["The 1st of every month", "0 3 1 * *"], ["When the server starts", "@reboot"]];
const CRON_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const HARBOR_JOBS = { backup_everything: "Backup", shrink_logs: "Log shrink" };

// A schedule in words, for the common ones; nothing for one of its own
function describeCron(expression) {
	const text = String(expression || "").trim().split(/\s+/).join(" ");
	const special = { "@reboot": "When the server starts", "@hourly": "Every hour", "@daily": "Every day at 00:00", "@midnight": "Every day at 00:00",
		"@weekly": "Every Sunday at 00:00", "@monthly": "The 1st of every month at 00:00", "@yearly": "Every 1 January at 00:00", "@annually": "Every 1 January at 00:00" };
	if (special[text]) return special[text];
	const fields = text.split(" ");
	if (fields.length !== 5) return "";
	const [minute, hour, day, month, weekday] = fields;
	const number = (value) => /^\d{1,2}$/.test(value);
	const every = (value) => (value.match(/^\*\/(\d+)$/) || [])[1];
	const time = (h, m) => String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
	const past = (m) => m === "0" ? "" : ` at :${m.padStart(2, "0")}`;
	const always = day === "*" && month === "*" && weekday === "*";
	if (minute === "*" && hour === "*") return always ? "Every minute" : "";
	if (every(minute) && hour === "*") return always ? `Every ${every(minute)} minutes` : "";
	if (number(minute) && hour === "*") return always ? "Every hour" + past(minute) : "";
	if (number(minute) && every(hour)) return always ? `Every ${every(hour)} hours` + past(minute) : "";
	let at;
	if (number(minute) && number(hour)) at = "at " + time(hour, minute);
	else if (number(minute) && /^\d{1,2}(,\d{1,2})+$/.test(hour)) {
		const times = hour.split(",").map((h) => time(h, minute));
		at = `at ${times.slice(0, -1).join(", ")} and ${times.at(-1)}`;
	} else return "";
	if (always) return "Every day " + at;
	if (day === "*" && month === "*" && number(weekday) && +weekday <= 7) return `Every ${CRON_DAYS[+weekday]} ` + at;
	if (day === "*" && month === "*" && weekday === "1-5") return "Every weekday " + at;
	if (number(day) && month === "*" && weekday === "*") return `Day ${day} of every month ` + at;
	return "";
}

async function openCrontab() {
	if (!crontabDialog.open) crontabDialog.showModal();
	showCrontabList();
	const list = $("#crontab-list");
	list.setAttribute("aria-busy", "true");
	if (!list.querySelector(".crontab-row")) list.replaceChildren(loadingState("Reading root's crontab…", "li"));
	try {
		crontabEntries = (await api("/api/server/crontab")).entries || [];
		renderCrontab();
	} catch (error) {
		list.replaceChildren(el("li", { className: "server-tools-loading", textContent: error.message }));
	} finally {
		list.removeAttribute("aria-busy");
	}
}

// Log under a job: when cron started it, and the end of its output with what looks like errors
async function showCronLog(row, entry, button) {
	const open = row.querySelector(".crontab-log");
	if (open) { open.remove(); button.setAttribute("aria-expanded", "false"); return; }
	button.setAttribute("aria-expanded", "true");
	const panel = el("div", { className: "crontab-log", ariaBusy: "true" }, loadingState("Reading the job's log…"));
	row.append(panel);
	let log;
	try {
		log = await api("/api/server/crontab/log", { json: { command: entry.command } });
	} catch (error) {
		panel.replaceChildren(el("p", { className: "error", textContent: error.message }));
		return;
	}
	panel.removeAttribute("aria-busy");
	const output = log.output || {};
	const ran = log.runs.length
		? `Ran ${log.runs_count} time${log.runs_count === 1 ? "" : "s"} in ${log.days} days · last ${formatDate(log.runs[0])}`
		: entry.schedule === "@reboot" ? `Not run in ${log.days} days: it runs when the server starts` : `Not run in the last ${log.days} days`;
	const verdict = !output.kind
		? ["", "Its output is not kept"]
		: log.errors.length ? ["bad", `${log.errors.length} line${log.errors.length === 1 ? " looks" : "s look"} like errors`] : ["ok", "No errors in its output"];
	panel.replaceChildren(
		el("div", { className: "crontab-log-summary" },
			el("span", { className: "tool-state " + (log.runs.length ? "ok" : "warn"), textContent: ran }),
			el("span", { className: "tool-state " + verdict[0], textContent: verdict[1] })),
		...(log.runs.length > 1 ? [el("p", { className: "crontab-log-runs muted", textContent: "Before: " + log.runs.slice(1, 6).map(formatDate).join(" · ") })] : []),
		...(log.errors.length ? [el("div", { className: "crontab-log-block" }, el("h4", { textContent: "What looks like errors" }),
			el("pre", { className: "crontab-log-errors", textContent: log.errors.join("\n") }))] : []),
		output.kind
			? el("div", { className: "crontab-log-block" },
				el("h4", { textContent: output.kind === "file"
					? `The end of ${output.where}` + (output.modified ? ` · written ${formatDate(output.modified * 1000)}` : " · not there yet")
					: `The end of its output in the journal (logger -t ${output.where})` }),
				el("pre", { className: "crontab-log-output", textContent: output.lines.length ? output.lines.join("\n") : "Nothing yet." }))
			: el("p", { className: "hint", textContent: "Its output is not written anywhere. End the command with >> /var/log/name.log 2>&1 to keep it, and see here what went wrong." }),
		el("p", { className: "hint", textContent: "cron does not record how a job ended: its output is what tells whether it went well." }));
	const pre = panel.querySelector(".crontab-log-output");
	if (pre) pre.scrollTop = pre.scrollHeight;
}

function renderCrontab() {
	const jobs = crontabEntries.map((entry, index) => [entry, index]).filter(([entry]) => entry.kind === "job");
	const kept = crontabEntries.length - jobs.length;
	$("#crontab-count").textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}` + (kept ? ` · ${kept} other line${kept === 1 ? "" : "s"} (variables, comments) kept as they are` : "");
	$("#crontab-list").replaceChildren(...(jobs.length ? jobs.map(([entry, index]) => { const row = el("li", { className: "crontab-row" },
		el("span", { className: "crontab-icon" }, svgIcon(HARBOR_JOBS[entry.harbor] ? "database" : "clock")),
		el("div", { className: "crontab-text" },
			el("div", { className: "crontab-when" }, el("strong", { textContent: describeCron(entry.schedule) || "A schedule of its own" }),
				el("code", { textContent: entry.schedule }),
				...(HARBOR_JOBS[entry.harbor] ? [el("span", { className: "badge", textContent: HARBOR_JOBS[entry.harbor] })] : [])),
			el("code", { className: "crontab-command", textContent: entry.command, title: entry.command })),
		el("div", { className: "crontab-actions" },
			Object.assign(toolButton("Log", () => showCronLog(row, entry, row.querySelector(".crontab-log-button")), "ghost crontab-log-button"), { ariaExpanded: "false" }),
			toolButton("Edit", () => showCronForm(index), "ghost"),
			toolButton("Delete", () => deleteCronJob(index), "ghost tool-danger"))); return row; })
		: [el("li", { className: "server-tools-loading", textContent: "No scheduled jobs yet." })]));
}

function showCrontabList() {
	$("#crontab-form").hidden = true;
	$("#crontab-list").hidden = false;
	$("#crontab-add").hidden = false;
}

async function saveCrontab(entries, message) {
	await api("/api/server/crontab", { json: { entries } });
	toast(message);
	await openCrontab();
}

async function deleteCronJob(index) {
	const entry = crontabEntries[index];
	if (!await ask({ title: "Delete this job?", message: `${describeCron(entry.schedule) || entry.schedule}: ${entry.command}`, input: false, ok: "Delete", danger: true })) return;
	await saveCrontab(crontabEntries.filter((_, other) => other !== index), "The job is deleted.");
}

// A path for the shell: as it is when it has nothing the shell reads, otherwise in single quotes
const shellPath = (path) => /^[A-Za-z0-9_.\/+=:,@-]+$/.test(path) ? path : `'${path.replace(/'/g, "'\\''")}'`;
// The scripts a job can run by their interpreter, when they may not be run themselves
const CRON_INTERPRETERS = { sh: "/bin/bash", bash: "/bin/bash", py: "/usr/bin/python3", js: "/usr/bin/node", mjs: "/usr/bin/node",
	php: "/usr/bin/php", pl: "/usr/bin/perl" };
const fileExtension = (name) => name.includes(".") ? name.split(".").pop().toLowerCase() : "";
const cronRunnable = (item) => (item.permissions || "").charAt(3) === "x";
// What Browse… offers: a file that may be run (its owner may execute it), or a script one of the interpreters runs
const cronCanRun = (item) => !item.dir && (cronRunnable(item) || fileExtension(item.name) in CRON_INTERPRETERS);
// The command for a chosen file: run directly when it may be run, else by its interpreter, with its output kept in
// a log of its own name under /var/log
function cronCommandFor(path, item) {
	const name = path.split("/").pop();
	const log = (name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "-") || "job") + ".log";
	const interpreter = cronRunnable(item) ? "" : (CRON_INTERPRETERS[fileExtension(name)] || "") + (CRON_INTERPRETERS[fileExtension(name)] ? " " : "");
	// cron reads % as a new line, also in quotes: \% keeps it
	return `${interpreter}${shellPath(path).replace(/%/g, "\\%")} >> /var/log/${log} 2>&1`;
}

// The file a command runs, read as the shell reads its words ("/etc/acmesh"/acme.sh is /etc/acmesh/acme.sh): its first
// word, or the script after an interpreter (/bin/bash /root/x.sh). Empty when it is not a full path
function cronProgramPath(text) {
	const words = [];
	let word = null, quote = "";
	for (let i = 0; i < text.length; i++) {
		const char = text[i];
		if (quote) {
			if (char === quote) quote = "";
			else if (char === "\\" && quote === '"' && i + 1 < text.length) word += text[++i];
			else word += char;
		} else if (/\s/.test(char)) {
			if (word !== null) words.push(word);
			word = null;
		} else if (/[;&|<>]/.test(char)) {
			break;
		} else if (char === "'" || char === '"') {
			quote = char; word = word ?? "";
		} else if (char === "\\" && i + 1 < text.length) {
			word = (word ?? "") + text[++i];
		} else {
			word = (word ?? "") + char;
		}
		if (words.length > 1) break;
	}
	if (word !== null && words.length < 2) words.push(word);
	const [first = "", second = ""] = words;
	const interpreter = /^(?:.*\/)?(?:bash|sh|python3?|node|php|perl)$/.test(first);
	const path = interpreter && second.startsWith("/") ? second : first;
	return path.startsWith("/") ? path : "";
}

// Browse… beside the command: the server's folders, unfolded under it, to choose the file the job runs
function cronBrowser(command) {
	const list = el("ul", { className: "crontab-browse-list" });
	const where = el("code", { className: "crontab-browse-path" });
	const panel = el("div", { className: "crontab-browse", hidden: true },
		el("div", { className: "crontab-browse-head" }, where,
			el("button", { type: "button", className: "cancel-link", textContent: "Close", onclick: () => { panel.hidden = true; } })),
		list);
	const open = async (folder) => {
		let data;
		try {
			data = await api("/api/server-fs/list?path=" + encodeURIComponent(folder));
		} catch (error) {
			// A folder that is not there (as /root/scripts on a new server): its parent, up to /
			if (folder) return open(folder.split("/").slice(0, -1).join("/"));
			throw error;
		}
		const path = data.path;
		where.textContent = "/" + path;
		// Folders, and the files a job can run: no web pages, images or configs
		const items = data.items.filter((item) => item.dir || cronCanRun(item)).sort((a, b) => (b.dir - a.dir) || a.name.localeCompare(b.name));
		const row = (label, icon, onclick, muted = false) => el("li", {}, el("button", { type: "button", className: "crontab-browse-item" + (muted ? " muted" : ""), onclick },
			svgIcon(icon), el("span", { textContent: label })));
		list.replaceChildren(
			...(path ? [row("..", "back", () => run(() => open(path.split("/").slice(0, -1).join("/"))), true)] : []),
			...items.map((item) => {
				const full = (path ? "/" + path : "") + "/" + item.name;
				return item.dir
					? row(item.name, "folder", () => run(() => open(full.slice(1))))
					: row(item.name, "file", () => { command.value = cronCommandFor(full, item); panel.hidden = true; command.focus(); });
			}));
		if (!items.length) list.append(el("li", { className: "muted crontab-browse-empty", textContent: "No scripts or programs in this folder" }));
	};
	const button = el("button", { type: "button", textContent: "Browse…", onclick: () => run(async () => {
		if (!panel.hidden) { panel.hidden = true; return; }
		// From the folder of the file the job runs now, or /root/scripts
		const current = cronProgramPath(command.value);
		await open(current ? current.slice(1).split("/").slice(0, -1).join("/") : "root/scripts");
		panel.hidden = false;
	}) });
	return { button, panel };
}

function showCronForm(index = null) {
	const entry = index === null ? { schedule: "0 3 * * *", command: "" } : crontabEntries[index];
	const form = $("#crontab-form");
	const when = el("input", { name: "schedule", value: entry.schedule, required: true, spellcheck: false, autocomplete: "off", className: "mono" });
	const said = el("p", { className: "hint crontab-said" });
	const describe = () => { said.textContent = describeCron(when.value) || "A schedule of its own: minute, hour, day of the month, month and day of the week, such as 15 */4 * * *"; };
	when.oninput = describe;
	describe();
	const command = el("input", { name: "command", value: entry.command, required: true, spellcheck: false, autocomplete: "off", className: "mono",
		placeholder: "/root/scripts/cleanup.sh >> /var/log/cleanup.log 2>&1" });
	const browse = cronBrowser(command);
	form.replaceChildren(
		el("h3", { textContent: index === null ? "Add a job" : "Edit the job" }),
		el("label", {}, "When", when),
		el("div", { className: "crontab-presets" }, ...CRON_PRESETS.map(([label, value]) => el("button", { type: "button", className: "chip", textContent: label,
			onclick: () => { when.value = value; describe(); when.focus(); } }))),
		said,
		el("label", {}, "Command", el("span", { className: "crontab-command-field" }, command, browse.button)),
		browse.panel,
		el("p", { className: "hint", textContent: "Browse… shows the scripts and programs a job can run, and keeps the output in a log of the same name under /var/log." }),
		el("div", { className: "crontab-form-actions" },
			el("button", { type: "button", className: "cancel-link", textContent: "Cancel", onclick: showCrontabList }),
			el("button", { type: "submit", className: "primary", textContent: index === null ? "Add job" : "Save" })));
	form.onsubmit = (event) => {
		event.preventDefault();
		const job = { kind: "job", schedule: when.value.trim(), command: command.value.trim() };
		const entries = [...crontabEntries];
		if (index === null) entries.push(job); else entries[index] = job;
		run(() => saveCrontab(entries, index === null ? "The job is added." : "The job is saved."));
	};
	$("#crontab-list").hidden = true;
	$("#crontab-add").hidden = true;
	form.hidden = false;
	(index === null ? when : command).focus();
}
$("#crontab-add").onclick = () => showCronForm();
$("#crontab-refresh").onclick = () => run(openCrontab);
$("#crontab-close").onclick = () => crontabDialog.close();
// The backups' state may have changed: Server tools shows it again
crontabDialog.addEventListener("close", () => { if (serverToolsDialog.open) run(openServerTools); });

// The dialog for an action that needs answers, then its task
function askServerTool(tool, options = {}) {
	const form = $("#form-server-action");
	const fields = $("#server-action-fields");
	const setup = {
		sql_install: ["database", "Install SQL Server", "The newest SQL Server for this Ubuntu, set up without questions.", "Install", () => [
			...(options.older ? [el("p", { className: "server-action-note", textContent: `Microsoft has no SQL Server for Ubuntu ${options.ubuntu || "this version"} yet. `
				+ `It is installed from the packages for Ubuntu ${options.older}: SQL Server runs on this Ubuntu that way, but Microsoft does not support it.` })] : []),
			choiceCards("edition", "Edition", SQL_EDITIONS, "Developer"),
			...passwordFields(),
			el("p", { className: "hint", textContent: "At least 8 characters, with three of: upper case, lower case, digits and symbols. The backups and database scripts use it too." }),
			checkOption("with_tools", "Install the SQL tools (sqlcmd)", "Needed for backups and the database scripts", true, "terminal", "blue"),
		]],
		sql_uninstall: ["database", "Remove SQL Server", "The service stops and SQL Server goes from this server.", "Remove SQL Server", () => [
			checkOption("with_tools", "Remove the SQL tools (sqlcmd) too", "Keep them to reach SQL Servers elsewhere", false, "terminal", "amber"),
			checkOption("delete_data", "Delete the databases, logs and backups", "Everything in /var/opt/mssql. Kept when not chosen.", false, "trash", "red"),
			el("div", { className: "server-action-danger", hidden: true, id: "server-action-delete" },
				el("p", { className: "server-action-confirm" }, "Type ", el("button", {
					type: "button", className: "confirmation-copy", textContent: "DELETE", title: "Copy confirmation text", ariaLabel: "Copy confirmation text: DELETE",
					onclick: async () => {
						try { await copyText("DELETE", serverActionDialog); toast("Copied to clipboard."); } catch (error) { toast(error.message, "error"); }
						form.elements.confirmation.focus({ preventScroll: true });
					},
				}), " to delete the databases, logs and backups"),
				el("input", { name: "confirmation", autocomplete: "off", spellcheck: false, "aria-label": "Type DELETE to delete the databases" })),
		]],
		ssh_root: ["terminal", "Root login over SSH", "How root may log in to this server over SSH. Connections open now stay.", "Save", () => {
			const choice = choiceCards("mode", "Root may log in", [["yes", "With a password or a key"], ["prohibit-password", "With a key only (recommended)"], ["no", "Not at all"]], options.current || "prohibit-password");
			const note = el("p", { className: "server-action-note", hidden: true, textContent: "Servers that manage this one over SSH as root (its environments elsewhere, Copy site, Move) cannot reach it any more, and neither can you as root. Keep another way in before you turn it off." });
			choice.addEventListener("change", () => { note.hidden = choice.querySelector("input:checked")?.value !== "no"; });
			return [choice, note];
		}],
		samba_on: ["server", "Share the server over Samba", "The whole server (/) as root, for root with a Samba password of its own. Anyone with that password can change anything on the server.", "Share the server", () =>
			options.hasPassword
				? [el("p", { className: "hint", textContent: "Root has a Samba password already: it is kept. Give a new one here to change it." }), ...sambaPasswordFields(false, "Samba password for root")]
				: sambaPasswordFields(true, "Samba password for root")],
		samba_save: ["server", options.share ? `Edit the share ${options.share.name}` : "Add a share", "A folder of this server shared over Samba. Root and its Samba password reach it, unless it lets guests in.", options.share ? "Save" : "Add share", () => {
			const share = options.share || { browseable: true, force_user: "root" };
			const input = (name, label, value, attributes = {}) => el("label", {}, label, el("input", { name, value: value || "", autocomplete: "off", spellcheck: false, ...attributes }));
			// Its files written as: one of three, side by side (a segmented choice, not a list to open)
			const as = (share.force_user ?? "root") || "";
			const writtenAs = el("div", { className: "samba-field" }, el("span", { className: "samba-field-label", textContent: "Files are written as" }),
				el("div", { className: "samba-segments", role: "radiogroup", ariaLabel: "Files are written as" },
					...[["root", "root", "Everything"], ["www-data", "www-data", "As the sites"], ["", "Who logs in", "Their own user"]].map(([value, title, sub]) =>
						el("label", { className: "samba-segment" }, el("input", { type: "radio", name: "share_force_user", value, checked: as === value }),
							el("strong", { textContent: title }), el("small", { textContent: sub })))));
			// Who may log in: each user a chip, on or off. None on lets every Samba user in.
			const users = options.users || [];
			const chosen = share.valid_users ? share.valid_users.split(/[\s,]+/) : null;
			const who = users.length > 1 ? [el("div", { className: "samba-field" }, el("span", { className: "samba-field-label", textContent: "Who may log in" }),
				el("div", { className: "samba-chips" }, ...users.map((user) => el("label", { className: "samba-chip" },
					el("input", { type: "checkbox", name: "share_user", value: user.name, checked: chosen ? chosen.includes(user.name) : user.name === "root" }),
					svgIcon("check"), el("span", { textContent: user.name })))),
				el("p", { className: "hint", textContent: "With none chosen, every Samba user may log in." }))] : [];
			return [
				el("div", { className: "ns-grid" },
					input("share_name", "Name", share.name, { required: true, maxLength: 80, pattern: "[A-Za-z0-9\\/][A-Za-z0-9._ $\\/\\-]*", title: "Letters, digits, / . - _ $ and spaces, starting with a letter, digit or /", placeholder: "www" }),
					input("share_path", "Folder", share.path, { required: true, placeholder: "/var/www", className: "mono" })),
				writtenAs,
				el("div", { className: "samba-options" },
					checkOption("share_read_only", "Read only", "No changes", !!share.read_only, "lock", "blue"),
					checkOption("share_hidden", "Hidden", "Not listed", share.browseable === false, "eye-off", "purple"),
					checkOption("share_guest_ok", "No login", "Open to all", !!share.guest_ok, "users", "red")),
				...who,
				input("share_comment", "Description (optional)", share.comment, { maxLength: 200 }),
			];
		}],
		samba_add_user: ["users", "Add a Samba user", "A login for the shares alone: its Linux user is made without a shell or a home, so it cannot log in to the server. It reaches no share until one lets it in.", "Add user", () => [
			el("label", {}, "User name", el("input", { name: "name", required: true, maxLength: 32, pattern: "[a-z_][a-z0-9_\\-]*", autocomplete: "off", spellcheck: false, placeholder: "web" })),
			...sambaPasswordFields(true)]],
		samba_user_password: ["key", `${options.name}'s Samba password`, "The password it reaches the shares with.", "Save password", () => sambaPasswordFields(true)],
		samba_password: ["key", "Root's Samba password", "The password root reaches the share with. It is not root's password on the server.", "Save password", () => sambaPasswordFields(true, "Samba password for root")],
		sa_password: ["key", "sa password", "The password of the sa login on this SQL Server, for the backups and database scripts. It is checked against the server first.", "Save password", passwordFields],
		dotnet_install: ["dotnet", "Add .NET", "Its SDK and ASP.NET Core runtime, beside the versions there.", "Install", () => [
			choiceCards("channel", "Version", (options.channels || []).map((channel) => [channel, channel === "8.0" || channel === "10.0" ? "Long-term support" : "Standard-term support"]), (options.channels || [])[0]),
		]],
		node_install: ["hexagon", options.current ? "Change Node.js version" : "Install Node.js", options.current
			? `One version at a time: ${options.current} is removed and the chosen one put in its place. Packages installed with npm -g stay.`
			: "From NodeSource's repository.", options.current ? "Change version" : "Install", () => {
			// Changing, nothing is chosen for you; installing, the newest
			const versions = choiceCards("major", "Version", NODE_MAJORS.map((major) => [major, major === options.current ? "Installed now" : "LTS"]), options.current ? null : NODE_MAJORS[0]);
			const sites = options.sites || [];
			if (!sites.length) return [versions];
			// What the change means for the sites that use Node.js: whether the versions they want include the chosen one
			const impact = el("div", { className: "tool-impact" });
			const show = () => {
				const major = versions.querySelector("input:checked")?.value || "";
				const misfits = major ? sites.filter((site) => nodeEnginesAllow(site.engines, major) === false).length : 0;
				impact.replaceChildren(
					el("p", { className: misfits ? "server-action-note" : "hint", textContent: !major
						? `${sites.length} site${sites.length === 1 ? " uses" : "s use"} Node.js. Choose a version to see whether it is one they want.`
						: misfits
						? `${misfits} of the ${sites.length} sites that use Node.js want${misfits === 1 ? "s" : ""} another version than ${major}. Their builds may fail until their package.json allows it.`
						: `${sites.length} site${sites.length === 1 ? " uses" : "s use"} Node.js. Build them after the change, and restart a service that runs node.` }),
					toolUsage(sites, { major, installed: options.current }));
			};
			versions.addEventListener("change", show);
			show();
			return [versions, impact];
		}],
	}[tool];
	const [icon, title, text, ok, build] = setup;
	$("#server-action-icon").replaceChildren(svgIcon(icon));
	$("#server-action-title").textContent = title;
	$("#server-action-text").textContent = text;
	const button = $("#server-action-ok");
	button.textContent = ok;
	// A dangerous action is red, never also the green primary
	button.classList.toggle("danger", tool === "sql_uninstall");
	button.classList.toggle("primary", tool !== "sql_uninstall");
	fields.replaceChildren(...build());
	const deleting = form.elements.delete_data;
	// As the other confirmations: the button stays off until the word is typed exactly
	const confirmed = () => { button.disabled = Boolean(deleting && deleting.checked && form.elements.confirmation.value !== "DELETE"); };
	button.disabled = false;
	if (deleting) {
		deleting.onchange = () => {
			$("#server-action-delete").hidden = !deleting.checked;
			form.elements.confirmation.required = deleting.checked;
			confirmed();
			if (deleting.checked) form.elements.confirmation.focus();
		};
		form.elements.confirmation.oninput = confirmed;
	}
	form.onsubmit = (event) => {
		event.preventDefault();
		const values = form.elements;
		if (values.samba_repeat && values.samba_password.value !== values.samba_repeat.value) {
			values.samba_repeat.setCustomValidity("The passwords are not the same");
			values.samba_repeat.reportValidity();
			values.samba_repeat.oninput = () => values.samba_repeat.setCustomValidity("");
			return;
		}
		if (values.sa_repeat && values.sa_password.value !== values.sa_repeat.value) {
			values.sa_repeat.setCustomValidity("The passwords are not the same");
			values.sa_repeat.reportValidity();
			values.sa_repeat.oninput = () => values.sa_repeat.setCustomValidity("");
			return;
		}
		if (values.confirmation && values.delete_data.checked && values.confirmation.value !== "DELETE") {
			values.confirmation.setCustomValidity("Type DELETE");
			values.confirmation.reportValidity();
			values.confirmation.oninput = () => values.confirmation.setCustomValidity("");
			return;
		}
		const payload = { tool };
		if (tool === "samba_add_user") payload.name = values.namedItem("name").value.trim();
		if (tool === "samba_user_password") payload.name = options.name;
		if (tool === "samba_save") {
			payload.old_name = options.share?.name || "";
			payload.share = { name: values.share_name.value.trim(), path: values.share_path.value.trim(),
				read_only: values.share_read_only.checked, browseable: !values.share_hidden.checked, guest_ok: values.share_guest_ok.checked,
				comment: values.share_comment.value.trim(), force_user: form.querySelector("[name=share_force_user]:checked")?.value ?? "root",
				valid_users: (options.users || []).length > 1
					? [...form.querySelectorAll("[name=share_user]:checked")].map((box) => box.value).join(" ")
					: options.share?.valid_users || "" };
		}
		for (const name of ["edition", "channel", "major", "mode", "sa_password", "samba_password", "confirmation"]) if (values[name]?.value) payload[name] = values[name].value;
		for (const name of ["with_tools", "delete_data"]) if (values[name]) payload[name] = values[name].checked;
		if (tool === "sql_install" && options.older) payload.older_ubuntu = true;
		serverActionDialog.close();
		run(() => runServerTool(payload));
	};
	serverActionDialog.showModal();
	// A field to type in gets the focus; otherwise Cancel, so no option looks focused (or chosen) as it opens
	const typed = [...fields.querySelectorAll("input:not([type=radio]):not([type=checkbox])")].find((input) => input.offsetParent !== null);
	(typed || $("#server-action-cancel")).focus();
}
$("#server-action-cancel").onclick = () => serverActionDialog.close();


// Enter or Escape closes the terminals when the one shown has ended, also for a terminal that takes no keys (a user's
// task), where the terminal itself never sees them
document.addEventListener("keydown", (event) => {
	if (event.key !== "Enter" && event.key !== "Escape") return;
	const t = terminals[activeTerminal];
	if (!t || !t.exited || $("#dock").hidden || document.querySelector("dialog[open]")) return;
	if (event.target instanceof HTMLElement && event.target.closest("input, textarea, select") && !event.target.closest(".xterm")) return;
	event.preventDefault();
	closeDock();
});


// ------------------------------------------------------------------ servers (harbor servers)
// Other servers Harbor works with: added with a password once, then reached with Harbor's own key
const serversDialog = $("#dlg-servers");

async function openServers() {
	if (!serversDialog.open) serversDialog.showModal();
	const list = $("#servers-list");
	list.setAttribute("aria-busy", "true");
	if (!list.childElementCount) list.replaceChildren(loadingState("Asking the servers…"));
	try {
		const data = await api("/api/servers");
		$("#servers-release").textContent = data.release ? `This server runs Harbor ${data.release}` : "Other servers this Harbor works with";
		const others = data.servers.filter((item) => !item.this).length;
		$("#servers-count").textContent = `${others} other server${others === 1 ? "" : "s"}`;
		// Linux and Windows apart, each under a heading with its count: this server first among the Linux ones
		const windows = data.servers.filter((item) => item.kind === "windows");
		const linux = data.servers.filter((item) => item.kind !== "windows").sort((a, b) => (b.this ? 1 : 0) - (a.this ? 1 : 0));
		const group = (icon, title, sub, items) => [el("div", { className: "servers-group" },
			el("span", { className: "servers-group-icon" }, svgIcon(icon)),
			el("span", { className: "servers-group-title", textContent: title }),
			el("span", { className: "servers-group-count", textContent: String(items.length) }),
			el("span", { className: "servers-group-sub", textContent: sub })),
			...items.map((item) => serverCard(item, data.release))];
		list.replaceChildren(...group("server", "Linux", "Harbor runs there: it installs and updates it from here", linux),
			...(windows.length ? group("windows", "Windows", "Shares and their logins, to import and copy sites from", windows) : []),
			...(data.servers.some((item) => !item.this) ? [] : [el("div", { className: "server-tools-loading", textContent: "No other servers yet. Add one to install and update Harbor there from here." })]));
	} catch (error) {
		list.replaceChildren(el("div", { className: "server-tools-loading", textContent: error.message }));
	} finally {
		list.removeAttribute("aria-busy");
	}
}
$("#servers-close").onclick = () => serversDialog.close();
$("#servers-refresh").onclick = () => openServers();
$("#servers-add").onclick = () => askAddServer();

// A release's version: 1.3.0 of 1.3.0-05abd71e5e08-dev. Builds of the same version (a test build here) are not an update.
function releaseVersion(name) {
	return String(name || "").split("-")[0].split(".").map((part) => parseInt(part, 10) || 0);
}

// Backoffice login needs the VNS.Harbor package, which needs Umbraco 18.3.1 or later: a site with an older Umbraco does
// not get the button, unless it has the package already
const UMBRACO_FOR_PACKAGE = [18, 3, 1];
function backofficeLoginFits(site) {
	if (site.harbor_package) return true;
	const version = String(site.umbraco_version || "").split(/[.-]/).map((part) => Number.parseInt(part, 10) || 0);
	return Boolean(site.umbraco_version) && compareVersions(version, UMBRACO_FOR_PACKAGE) >= 0;
}

function compareVersions(a, b) {
	for (let index = 0; index < Math.max(a.length, b.length); index++) {
		const difference = (a[index] || 0) - (b[index] || 0);
		if (difference) return difference;
	}
	return 0;
}

function serverCard(item, release) {
	// A Windows server: its share and login, for environments on it and Import from Windows, with the Windows logo. It
	// stays while an environment here runs on it: what runs where is known from Servers.
	if (item.kind === "windows") {
		const remove = toolButton("Remove…", () => askRemoveServer(item), "tool-danger");
		const card = toolCard({ icon: "server", tone: item.reachable ? "blue" : "amber", title: item.name, detail: item.share,
			state: item.reachable ? "Share opens" : "Not reachable", stateClass: item.reachable ? "ok" : "bad",
			rows: [toolRow("Login", (item.domain ? item.domain + "\\" : "") + item.user),
				...(item.reachable ? [] : [toolRow("Error", (item.error || []).join(" ") || "No answer")])],
			actions: [toolButton("Edit…", () => askEditServer(item)), remove] });
		card.querySelector(".tool-icon").innerHTML = osLogos.windows;
		card.querySelector(".tool-icon").classList.add("os-icon");
		return card;
	}
	const harbor = item.harbor && item.harbor.name;
	const order = harbor && release ? compareVersions(releaseVersion(release), releaseVersion(harbor)) : 0;
	const current = harbor && order <= 0;
	const state = !item.reachable ? ["Not reachable", "bad"] : !item.compatible ? ["Cannot run Harbor", "bad"]
		: !harbor ? ["No Harbor", "warn"] : order < 0 ? ["Newer than this server", "ok"] : current ? ["Up to date", "ok"] : ["Update available", "warn"];
	const rows = item.reachable ? [
		toolRow("System", `${item.os || "—"} · Python ${item.python || "—"}`),
		toolRow("Harbor", harbor || "Not installed", { on: Boolean(harbor) }),
		...(item.vns ? [toolRow("Old installation", item.vns + (item.admin ? ` · admin ${item.admin}` : ""))] : []),
		...(item.problems || []).map((problem) => toolRow("Needs", problem)),
	] : [toolRow("Error", (item.error || []).join(" ") || "No answer")];
	if (item.this) {
		return toolCard({ icon: "server", tone: "purple", title: item.name, detail: item.host || "Installs and updates the others",
			state: "This server", stateClass: "ok", rows,
			actions: item.rollback ? [toolButton(`Roll back to ${item.rollback.version}`, () => rollbackHarbor(item.rollback))] : [] });
	}
	const actions = [];
	if (item.reachable && item.compatible && release && !current) {
		actions.push(toolButton(harbor ? `Update to ${release.split("-")[0]}` : "Install Harbor", () => runServers({ action: "install", name: item.name }), "primary"));
	}
	// The release installed there before the one in use, while it is still there
	if (item.reachable && item.rollback) {
		actions.push(toolButton(`Roll back to ${item.rollback.version}`, async () => {
			if (!(await ask({ title: `Roll back ${item.name} to ${item.rollback.version}?`,
				message: `${item.name} goes back to Harbor ${item.rollback.name}, the release installed there before ${harbor}. Its sites keep running.`,
				input: false, ok: "Roll back" }))) return;
			await runServers({ action: "rollback", name: item.name });
		}));
	}
	actions.push(toolButton("Edit…", () => askEditServer(item)));
	actions.push(toolButton("Remove…", () => askRemoveServer(item), "tool-danger"));
	return toolCard({ icon: "server", tone: item.reachable ? "blue" : "amber", title: item.name, detail: `${item.user}@${item.host}${item.port !== 22 ? ":" + item.port : ""}`,
		state: state[0], stateClass: state[1], rows, actions });
}

async function runServers(payload) {
	serversDialog.close();
	const t = await openTask({ kind: "servers", ...payload });
	if (t) t.afterTask = openServers;
}

async function confirmServers(title, message, ok, payload) {
	if (await ask({ title, message, input: false, ok, danger: true })) await runServers(payload);
}

// A server's name in Servers, and a Linux server's address: for one given another IP, such as the one that takes over a
// Windows server's address. Also when the old address no longer answers
function askEditServer(item) {
	const form = $("#form-server-action");
	const windows = item.kind === "windows";
	$("#server-action-icon").replaceChildren(svgIcon("server"));
	$("#server-action-title").textContent = `Edit ${item.name}`;
	$("#server-action-text").textContent = `Nothing changes on ${item.name} itself.`;
	const button = $("#server-action-ok");
	button.textContent = "Save";
	button.classList.remove("danger");
	button.classList.add("primary");
	const field = (label, props) => el("label", {}, label, el("input", { autocomplete: "off", spellcheck: false, required: true, ...props }));
	$("#server-action-fields").replaceChildren(
		field("Name", { name: "new_name", value: item.name, pattern: "[a-z0-9][a-z0-9\\-]{0,62}", title: "Lower case letters, digits and -" }),
		...(windows ? [] : [field("Address", { name: "host", value: item.host }),
			el("p", { className: "hint", textContent: `A new address: Harbor logs in there with its key, then changes it here and in every environment on ${item.host}, so publish, refresh and take over go on there.` })]));
	form.onsubmit = (event) => {
		event.preventDefault();
		const name = form.elements.new_name.value.trim();
		const host = windows ? "" : form.elements.host.value.trim();
		const payload = { action: "edit", name: item.name, new_name: name !== item.name ? name : "", host: host !== item.host ? host : "" };
		serverActionDialog.close();
		if (payload.new_name || payload.host) run(() => runServers(payload));
	};
	serverActionDialog.showModal();
	form.elements.new_name.focus();
}

// The environments of sites here that run on a Linux server (by its address)
const linuxServerUsers = (item) => allSites.flatMap((site) => (site.environments || []).filter((env) => env.mode === "remote" && env.host === item.host)
	.map((env) => `${siteLabel(site)} (${environmentLabel(site, env)})`));
const windowsServerEnvs = (item) => {
	const norm = (text) => String(text || "").replace(/\\/g, "/").trim().replace(/\/+$/, "").toLowerCase();
	return allSites.flatMap((site) => (site.environments || [])
		.filter((env) => env.mode === "windows" && norm(env.folder) && (norm(env.folder) === norm(item.share) || norm(env.folder).startsWith(norm(item.share) + "/")))
		.map((env) => ({ site, env })));
};
const linuxServerEnvs = (item) => allSites.flatMap((site) => (site.environments || []).filter((env) => env.mode === "remote" && env.host === item.host)
	.map((env) => ({ site, env })));
// Remove: a server an environment here runs on stays in Servers. The dialog shows those environments (the link between
// this server and that one), each opening its site's page to move or remove it, and Remove is off until there are none.
function askRemoveServer(item) {
	const form = $("#form-server-action");
	const windows = item.kind === "windows";
	$("#server-action-icon").replaceChildren(svgIcon(windows ? "windows" : "server"));
	$("#server-action-title").textContent = `Remove ${item.name}`;
	const envs = windows ? windowsServerEnvs(item) : linuxServerEnvs(item);
	$("#server-action-text").textContent = envs.length
		? `${item.name} cannot be removed: environments here run on it. Move or remove them first.`
		: windows ? `Its share and login are forgotten here, with the password. Nothing on ${item.name} is changed.`
			: `Harbor forgets ${item.name} here. Its sites and its admin are never touched.`;
	const button = $("#server-action-ok");
	button.textContent = "Remove server";
	button.classList.add("danger");
	button.classList.remove("primary");
	button.disabled = envs.length > 0;
	// The dialog is shared: its button is on again for the next one
	serverActionDialog.addEventListener("close", () => { button.disabled = false; }, { once: true });
	const reachable = item.reachable;
	const fields = [];
	if (envs.length) {
		fields.push(el("div", { className: "ns-section", textContent: "Used here by" }),
			el("div", { className: "used-by-list" }, ...envs.map(({ site, env }) => el("button", {
				type: "button", className: "used-by-item", title: `Open ${siteLabel(site)}`,
				onclick: () => { serverActionDialog.close(); serversDialog.close(); if (isTarget(env)) unfoldedEnvs.add(site.name + "/" + (env.target || "prod")); go(siteLink(site.name)); },
			}, el("span", { className: "badge env-" + environmentLabel(site, env).toLowerCase(), textContent: environmentLabel(site, env) }),
				el("span", { className: "used-by-text" }, el("strong", { textContent: siteLabel(site) }), el("small", { textContent: bindingLabel(env) || env.folder || "" })),
				el("span", { className: "used-by-open", textContent: "Open" }, svgIcon("chevron"))))));
	} else if (!windows) {
		const sql = (item.keep_harbor || []).find((reason) => reason.startsWith("SQL connections"));
		fields.push(checkOption("uninstall", "Uninstall Harbor there", !item.harbor ? "Harbor is not installed there"
				: (item.own_sites || []).length ? "Not while it has sites of its own"
					: sql ? `Not while ${sql.replace(/^SQL connections here use it/, "SQL connections here use it")}` : `Harbor ${item.harbor.name} goes: /opt/harbor and the harbor command`, false, "trash", "red"),
			// Harbor's key goes with it: said, not asked
			el("p", { className: "hint", textContent: reachable ? `Harbor's key is taken off ${item.name}, so this server cannot log in there any more. Adding it again needs its password once.` : `${item.name} does not answer now, so it is only forgotten here, and Harbor's key stays there.` }));
	}
	$("#server-action-fields").replaceChildren(...fields);
	if (form.elements.uninstall) form.elements.uninstall.disabled = !reachable || !item.harbor || (item.keep_harbor || []).length > 0;
	form.onsubmit = (event) => {
		event.preventDefault();
		if (envs.length) return;
		const uninstall = Boolean(form.elements.uninstall?.checked);
		serverActionDialog.close();
		run(() => runServers({ action: "remove", name: item.name, uninstall }));
	};
	serverActionDialog.showModal();
	$("#server-action-cancel").focus();
}

// Add server: from Servers, or from a dialog that needs a server (Add environment, Copy site). From such a dialog the kind
// is chosen already, that dialog stays open under it, and after is run once the server is added, to choose it there.
function askAddServer({ kind: chosenKind = "linux", after = null } = {}) {
	const form = $("#form-server-action");
	$("#server-action-icon").replaceChildren(svgIcon("server"));
	$("#server-action-title").textContent = "Add server";
	$("#server-action-text").textContent = "Harbor logs in once with the password and puts its own SSH key there. The password is not kept.";
	const button = $("#server-action-ok");
	button.textContent = "Add server";
	button.classList.remove("danger");
	button.classList.add("primary");
	const field = (label, props) => el("label", {}, label, el("input", { autocomplete: "off", spellcheck: false, ...props }));
	// Linux: Harbor's key is put there. Windows: only its share and login are kept, for Import from Windows.
	const kind = (value, title, sub, checked) => el("label", { className: "type-card" },
		el("input", { type: "radio", name: "kind", value, checked, onchange: () => showKind() }),
		el("span", { className: "tc-title", textContent: title }), el("span", { className: "tc-sub", textContent: sub }));
	const linux = el("div", { className: "server-kind-fields" },
		el("div", { className: "ns-grid server-address-row" },
			field("Address", { name: "host", required: true, placeholder: "IP or host name", maxLength: 253 }),
			field("SSH port", { name: "port", value: "22", required: true, inputMode: "numeric", pattern: "[0-9]{1,5}" })),
		el("div", { className: "ns-grid" },
			field("Login", { name: "user", value: "root", required: true, maxLength: 32 }),
			field("Password", { name: "password", type: "password", autocomplete: "new-password", placeholder: "Used once, not kept", maxLength: 256 })),
		el("p", { className: "hint", textContent: "Leave the password empty when Harbor's key is in ~/.ssh/authorized_keys there already. A login other than root needs passwordless sudo." }));
	const windows = el("div", { className: "server-kind-fields" },
		field("Share", { name: "share", required: true, placeholder: "//server/share/folder", maxLength: 400 }),
		el("div", { className: "ns-grid" },
			field("Username", { name: "smb_user", required: true, maxLength: 128 }),
			field("Password", { name: "smb_password", type: "password", autocomplete: "new-password", required: true, maxLength: 256 })),
		field("Domain", { name: "domain", placeholder: "Optional", maxLength: 128 }),
		el("p", { className: "hint", textContent: "Harbor tries the login, then keeps the share and login: Windows then shows in the Server menu, with Import sites. The password is kept for root only; nothing is run on Windows." }));
	const showKind = () => {
		const isWindows = form.elements.kind.value === "windows";
		for (const [part, on] of [[linux, !isWindows], [windows, isWindows]]) {
			part.hidden = !on;
			for (const input of part.querySelectorAll("input")) input.disabled = !on;
		}
		$("#server-action-text").textContent = isWindows
			? "Its share and login are kept, so Import from Windows can choose it. Harbor runs nothing there."
			: "Harbor logs in once with the password and puts its own SSH key there. The password is not kept.";
	};
	$("#server-action-fields").replaceChildren(
		el("div", { className: "type-cards" }, kind("linux", "Linux", "Over SSH: Harbor installs and updates it", chosenKind === "linux"), kind("windows", "Windows", "A share and its login, for Import from Windows", chosenKind === "windows")),
		field("Name", { name: "name", required: true, pattern: "[a-z0-9][a-z0-9\\-]{0,62}", placeholder: "A short name for it, such as web-01", maxLength: 63 }),
		linux, windows);
	showKind();
	// A name as the server knows it: lower case, letters, digits and -, made so as it is typed (KIKAYO becomes kikayo)
	const name = form.elements.name;
	name.title = "Lower case letters, digits and -, such as web-01";
	name.oninput = () => {
		const at = name.selectionStart;
		name.value = name.value.toLowerCase().replace(/[^a-z0-9-]+/g, "-");
		name.setSelectionRange(at, at);
		name.setCustomValidity("");
	};
	name.oninvalid = () => name.setCustomValidity("Use lower case letters, digits and -, starting with a letter or digit, such as web-01.");
	form.onsubmit = (event) => {
		event.preventDefault();
		const values = form.elements;
		const payload = values.kind.value === "windows"
			? { action: "add_windows", name: values.name.value.trim(), share: values.share.value.trim(), user: values.smb_user.value.trim(),
				password: values.smb_password.value, domain: values.domain.value.trim() }
			: { action: "add" };
		if (values.kind.value !== "windows") for (const name of ["name", "host", "user", "port", "password"]) payload[name] = values[name].value.trim();
		serverActionDialog.close();
		if (after) {
			run(async () => {
				const t = await openTask({ kind: "servers", ...payload });
				if (t) t.afterTask = () => { if (t.taskState !== "failed") run(() => after(payload.name)); };
			});
		} else run(() => runServers(payload));
	};
	serverActionDialog.showModal();
	form.elements.name.focus();
}
