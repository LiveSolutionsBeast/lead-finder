/**
 * content.js — Lead Finder LinkedIn Injector content script
 * ===========================================================
 * Runs only on https://www.linkedin.com/in/* profile pages.
 *
 * Strategy (per user requirement — logged in with Premium, sees everything):
 *   1. PRIMARY: read the real DOM, not marketing meta tags. The user is
 *      logged in with Premium so the data is fully visible to them.
 *   2. The top card has the name (<h1 class="top-card-layout__title">) and
 *      the current company (data-section="currentPositionsDetails" → span).
 *   3. The Experience & Education section has the full history in
 *      <li class="profile-section-card"> items, each with an <h3> (company)
 *      and an <h4> (title). For 1st-degree connections and Premium users
 *      with "Who Viewed My Profile" access, titles are not blurred.
 *   4. The current title is often between the h1 and the company span
 *      (e.g. "EVP and Chief Financial Officer, The Boeing Company" appears
 *      as a subheading in the top card).
 *   5. FALLBACK: leave fields empty and let the user fill the form manually.
 *      The popup is ALWAYS shown per the user's requirement (no silent path).
 *
 * Notes:
 *   - DO NOT extract the headline (per user — unreliable personal descriptor).
 *   - MutationObserver + Re-scan button let the user retry extraction when
 *     LinkedIn's lazy-loaded content has finished rendering.
 *
 * Communicates with popup via chrome.runtime.sendMessage / onMessage.
 */

(() => {
  "use strict";

  // Build identifier — change this timestamp whenever the parser logic changes.
  // It appears in console logs and debug snapshots so we can verify which
  // version of content.js is actually running in Chrome.
  const BUILD_ID = "parser-20260725-2300";
  console.log(`[lead-finder/cs] BUILD_ID=${BUILD_ID}`);

  // ── Logging helper (so the user can open DevTools and see why extraction failed)
  const log = (...args) => console.log("[lead-finder/cs]", ...args);
  const warn = (...args) => console.warn("[lead-finder/cs]", ...args);

  // ── Slug extraction (mirror of server's _extract_linkedin_slug) ───────────
  const extractSlug = (url) => {
    try {
      const u = new URL(url);
      const m = u.pathname.match(/^\/in\/([^/?#]+)/);
      return m ? m[1] : "";
    } catch {
      return "";
    }
  };

  // ── Extract the full name from the profile ────────────────────────────────
  // The current LinkedIn DOM doesn't use <h1> for the name. The first
  // <h2> in <main> is the name. (LinkedIn uses semantic heading levels
  // and the profile name is the page's "title".)
  // Fall back to og:title if no <h2> is found.
  const extractName = () => {
    const mainH2 = document.querySelector("main h2");
    if (mainH2) {
      const t = (mainH2.textContent || "").trim();
      if (t) return t;
    }
    // Fallback: og:title (always present). Format: "Breslin Juan - LinkedIn"
    const og = document.querySelector('meta[property="og:title"]');
    if (og) {
      return (og.getAttribute("content") || "").replace(/\s*[|\-–]\s*LinkedIn\s*$/i, "").trim();
    }
    return "";
  };

  // ── Extract the current company from the top card ─────────────────────────
  // LinkedIn's modern top card has a "Current company" line below the name.
  // The text appears as the first segment in the profile header. We look
  // for the company link's text — usually a short string immediately after
  // the name h2.
  const extractCurrentCompanyFromTopCard = () => {
    const main = document.querySelector("main");
    if (!main) return "";
    // Walk the immediate text-bearing elements after the name h2.
    const nameH2 = main.querySelector("h2");
    if (!nameH2) return "";
    // The company is usually in the next sibling div/section.
    let node = nameH2.parentElement;
    while (node) {
      node = node.nextElementSibling;
      if (!node) break;
      const link = node.querySelector("a[href*='/company/']");
      if (link) {
        const text = (link.textContent || "").trim();
        // The link text usually contains the company name (possibly with
        // a logo alt text prefix). Take the longest meaningful chunk.
        const parts = text.split(/\s*[·•]\s*/).map(s => s.trim()).filter(Boolean);
        if (parts.length > 0) return parts[0];
      }
    }
    return "";
  };

  // ── Extract the location from the top card ─────────────────────────────────
  // Location appears in the top card near the bottom. Format is usually
  // "City, State, Country" or "City Metropolitan Area".
  //
  // CRITICAL: only look in the top card. The Experience section also has
  // "City, State" patterns in old roles (e.g. a previous job at Boeing in
  // "Everett, Washington" — the parser hit that first and overrode the
  // top card location with a stale old job's city). The fix: scope the
  // search to elements OUTSIDE the Experience <section>.
  const extractLocation = () => {
    const main = document.querySelector("main");
    if (!main) return "";
    // Find the Experience <h2> so we can exclude its <section>.
    const expH2 = Array.from(main.querySelectorAll("h2"))
      .find(h => {
        const t = (h.textContent || "").trim();
        return t === "Experience" || t.startsWith("Experience &") || t.startsWith("Experience,");
      });
    const expSection = expH2 ? (expH2.closest("section") || expH2.parentElement) : null;
    // Look for a text node matching a "City, State" or "Metropolitan Area"
    // pattern. Skip elements that are inside the Experience section.
    const candidates = main.querySelectorAll("span, div, p");
    for (const el of candidates) {
      // Skip if this element is inside the Experience section
      if (expSection && expSection.contains(el)) continue;
      const direct = Array.from(el.childNodes)
        .filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => (n.textContent || "").trim())
        .filter(Boolean)
        .join(" ");
      if (!direct) continue;
      // "City, State" or "City, State, Country"
      if (/^[A-Z][a-zA-Z\.\-]+(?:\s[A-Z][a-zA-Z\.\-]+)*(?:,\s*[A-Z][a-zA-Z\.\-]+){1,3}$/.test(direct)) {
        return direct;
      }
      // "Metropolitan Area" or "Greater City Area"
      if (/^[A-Z][a-zA-Z\.\-]+(?:\s[A-Z][a-zA-Z\.\-]+)*\s+(Metropolitan|Greater)\s+[A-Z]/.test(direct)) {
        return direct;
      }
    }
    return "";
  };

  // Parse a "Month YYYY" or "YYYY" string into a Date for recency sorting.
  const parseDate = (str) => {
    if (!str) return null;
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    const m = str.match(/^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$/);
    if (m) return new Date(parseInt(m[2], 10), months.indexOf(m[1]), 1);
    const y = str.match(/^(\d{4})$/);
    if (y) return new Date(parseInt(y[1], 10), 0, 1);
    return null;
  };

  // ── Find the LATEST experience role ───────────────────────────────────────
  // We need ONE role: the current role, or if none, the most recent one.
  // LinkedIn has two shapes we must handle:
  //   1. Flat role blocks (Nicole, Steven, Jay): each top-level entity item is
  //      one role. The company is represented by TWO anchors: a logo-only link
  //      (contains <figure>/<svg>/<img>, aria-label="X logo") and a text link
  //      (contains <p> or <span> with the visible company name). Title, dates,
  //      and location live in a sibling text-column <div>.
  //   2. Grouped roles (Jane): one company link at the group level, multiple
  //      roles inside a <ul>/<li> list. Each <li> is a distinct role with its
  //      own title/dates/location; the company name is inherited from the group.
  // Strategy:
  //   - Find the Experience section and its heading as before.
  //   - Find all top-level entries: `section.querySelectorAll('[componentkey^="entity-collection-item-"]')`.
  //   - For each entry:
  //       * logo link = entry.querySelector('a[href*="/company/"]:has(figure)')
  //       * text link = the other a[href*="/company/"] (or its first non-logo child)
  //       * grouped = entry.querySelector(':scope > div > ul, :scope ul') exists
  //   - For grouped entries, company comes from the first meaningful <p> inside
  //     the group text link that is not a tenure duration; the first <li> is the
  //     role container.
  //   - For flat entries, collect leaf text elements inside the text link, then
  //     identify date (regex), title (element before date), company (element
  //     before title; strip employment-type bullet), and location (element after
  //     date; strip work-mode bullet).
  //   - Pick current role first, then latest start date, then earliest DOM order.
  const findLatestExperienceItem = () => {
    const monthRe = /(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4}\s*[-–]/;
    const rangeRe = /((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4})\s*[-–]\s*(Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4})/i;
    const dateRe = /(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[-–]\s*(Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4})/i;
    const empTypes = ["Full-time", "Part-time", "Self-employed", "Contract", "Freelance", "Internship", "Apprenticeship", "Temporary", "Volunteer"];
    const workModes = ["On-site", "Remote", "Hybrid"];
    const durationRe = /\d+\s*(?:yrs?|mos?)\s*(?:\d+\s*mos?)?/i;
    const empSplitRe = new RegExp(`^(.*?)\\s*[·•-]\\s*(${empTypes.join("|")})\\s*$`, "i");
    const locationRe = /,\s*[A-Z]|\b(Metropolitan|Greater|Area|County|Region)\b/i;

    const expH2 = Array.from(document.querySelectorAll("h2"))
      .find(h => {
        const t = h.textContent.trim();
        return t === "Experience" || t.startsWith("Experience &") || t.startsWith("Experience,");
      });
    if (!expH2) return null;

    const section = expH2.closest("section") || expH2.parentElement;
    if (!section) return null;

    // Helper: is this element the logo-only company link?
    function isLogoLink(link) {
      const aria = (link.getAttribute("aria-label") || "").toLowerCase();
      const title = (link.getAttribute("title") || "").toLowerCase();
      if (aria.endsWith(" logo") || title.endsWith(" logo")) return true;
      const children = Array.from(link.children);
      if (!children.length) return false;
      if (children.every(c => /^(FIGURE|SVG|IMG|DIV)$/.test(c.tagName) && /figure|svg|img/i.test(c.className || c.tagName))) {
        // Contains only image-bearing children. Confirm it has no meaningful text.
        return !meaningfulLinkText(link);
      }
      return false;
    }

    function meaningfulLinkText(link) {
      const textEls = Array.from(link.querySelectorAll("p, span, div, li, figcaption")).filter(el => {
        if (el.querySelector("img, svg, figure")) return false;
        return true;
      });
      for (const el of textEls) {
        const t = (el.textContent || "").trim();
        if (t && t.length > 1 && !/\blogo\b/i.test(t) && !/^[\d\s·•-]+$/.test(t)) return t;
      }
      const direct = Array.from(link.childNodes)
        .filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => (n.textContent || "").trim())
        .filter(Boolean)
        .join(" ");
      if (direct && !/\blogo\b/i.test(direct)) return direct;
      return "";
    }

    function looksLikeDuration(s) {
      if (!s) return false;
      return /\b\d+\s*(yrs?|mos?)\b/i.test(s) || /^\d+\s*(?:yr|mo)(?:s?)?\s*(?:\d+\s*(?:yr|mo)(?:s?)?)?$/i.test(s.trim());
    }

    function looksLikeLocation(s) {
      if (!s || s.length > 200) return false;
      return locationRe.test(s);
    }

    function stripWorkMode(s) {
      const re = new RegExp(`^(.*?)\\s*[·•-]?\\s*(${workModes.join("|")})\\s*$`, "i");
      const m = s.match(re);
      return m ? m[1].trim() : s;
    }

    function fieldText(el) {
      return (el.textContent || "").replace(/\s+/g, " ").trim();
    }

    function leafTextElements(container) {
      // Return all <p> and <span> descendants whose text is not inside a logo/figure.
      const out = [];
      const all = Array.from(container.querySelectorAll("p, span"));
      for (const el of all) {
        if (el.closest("figure, svg, img, [aria-label$=' logo'], [title$=' logo']")) continue;
        const t = (el.textContent || "").trim();
        if (t && !/\blogo\b/i.test(t)) out.push(el);
      }
      return out;
    }

    function extractGroupCompany(textLink) {
      // The group text link contains company name + total tenure. Pick the first
      // meaningful leaf text element that is not a duration or date range.
      // We must use leaf elements only; wrapper <div>s can concatenate the
      // company name and tenure into an unparseable string (e.g. "Boeing8 yrs").
      for (const el of leafTextElements(textLink)) {
        const t = fieldText(el);
        if (!t || t.length < 2 || /\blogo\b/i.test(t)) continue;
        if (looksLikeDuration(t)) continue;
        // Also reject date ranges.
        if (dateRe.test(t)) continue;
        return t;
      }
      // Fallback: any meaningful link text that is not a duration.
      const mt = meaningfulLinkText(textLink);
      if (mt && !looksLikeDuration(mt)) return mt;
      return "";
    }

    // Flat DOM: the text-bearing /company/ link has 2-3 direct child blocks:
    //   1. title block (contains <p>Job Title</p> and <p>Company · Full-time</p>)
    //   2. dates block (<p>Dates · duration</p>)
    //   3. location block (<p>Location · work mode</p>) (optional)
    // We identify the date block first, then extract title/company from the
    // preceding block's leaf <p> elements, and location from the following block.
    function parseFlatEntry(entry, idx) {
      const links = Array.from(entry.querySelectorAll('a[href*="/company/"]'));
      let logoLink = links.find(isLogoLink);
      if (!logoLink) {
        // Use :has(figure) selector if supported; otherwise approximate.
        logoLink = entry.querySelector('a[href*="/company/"]:has(figure)');
      }
      let textLink = links.find(l => l !== logoLink && !isLogoLink(l) && meaningfulLinkText(l));
      if (!textLink) {
        // Fallback: first non-logo /company/ link.
        textLink = links.find(l => l !== logoLink && !isLogoLink(l));
      }
      if (!textLink) return null;

      // Unwrap single-child wrapper <div>s. Some profiles wrap the real
      // content container in one (or more) nested wrapper <div>s, so the
      // text link's only direct child is a single wrapper. Descend through
      // single-child wrappers to reach the container whose direct children
      // are [titleBlock, dateBlock, locationBlock]. We stop descending as
      // soon as there are multiple children, or the single child is a leaf
      // element (p/span/figure/svg/img) that should not be unwrapped.
      let blockRoot = textLink;
      for (let guard_ = 0; guard_ < 8; guard_++) {
        if (!blockRoot.children || blockRoot.children.length !== 1) break;
        const only = blockRoot.children[0];
        const tag = (only.tagName || "").toLowerCase();
        if (["p", "span", "figure", "svg", "img"].includes(tag)) break;
        if (!only.children || only.children.length === 0) break;
        blockRoot = only;
      }

      // Build ordered blocks from direct children of the resolved block root.
      const blocks = [];
      for (const child of blockRoot.children) {
        const t = fieldText(child);
        if (t) blocks.push(child);
      }
      // Identify the block containing the date range.
      let dateIdx = -1;
      for (let i = 0; i < blocks.length; i++) {
        if (dateRe.test(blocks[i].textContent || "")) {
          dateIdx = i;
          break;
        }
      }
      if (dateIdx < 0) return null;

      // Title + company are in the block immediately before the date block.
      let title = "";
      let company = "";
      if (dateIdx > 0) {
        const titleBlock = blocks[dateIdx - 1];
        const titleLeaves = leafTextElements(titleBlock);
        if (titleLeaves.length > 0) title = fieldText(titleLeaves[0]);
        if (titleLeaves.length > 1) {
          const companyLine = fieldText(titleLeaves[titleLeaves.length - 1]);
          const m = companyLine.match(empSplitRe);
          company = m ? m[1].trim() : companyLine;
        }
      }

      // Location: first leaf in the block after the date block.
      let location = "";
      if (dateIdx + 1 < blocks.length) {
        const locationBlock = blocks[dateIdx + 1];
        const locLeaves = leafTextElements(locationBlock);
        for (const leaf of locLeaves) {
          const t = fieldText(leaf);
          if (looksLikeLocation(t)) {
            location = stripWorkMode(t);
            break;
          }
        }
        if (!location && locLeaves.length > 0) {
          const first = fieldText(locLeaves[0]);
          if (looksLikeLocation(first)) location = stripWorkMode(first);
        }
      }

      const dateText = fieldText(blocks[dateIdx]);
      const text = [title, company, dateText, location].filter(Boolean).join(" ");

      return makeRoleResult(idx, text, entry, false, company, title, location);
    }

    // Grouped DOM: the top-level entity item contains a company text link and a
    // <ul> of roles. Company is extracted from the group text link; each <li>
    // holds its own title/dates/location.
    function parseGroupedEntry(entry, idx) {
      const ul = entry.querySelector(':scope > div > ul, :scope > ul, ul');
      if (!ul) return null;
      const roleLi = ul.querySelector(':scope > li');
      if (!roleLi) return null;

      const groupLinks = Array.from(entry.querySelectorAll('a[href*="/company/"]'));
      let textLink = groupLinks.find(l => !isLogoLink(l) && meaningfulLinkText(l));
      if (!textLink) textLink = groupLinks.find(l => !isLogoLink(l));
      const company = textLink ? extractGroupCompany(textLink) : "";

      // Some grouped roles put the work location in the company group header
      // alongside the company name and total tenure (e.g., "Los Angeles
      // Metropolitan Area · Hybrid"). Capture it as a fallback before scanning
      // the individual role <li>.
      let groupLocation = "";
      if (textLink) {
        for (const leaf of leafTextElements(textLink)) {
          const t = fieldText(leaf);
          if (!t) continue;
          if (looksLikeLocation(t)) {
            groupLocation = stripWorkMode(t);
            break;
          }
        }
      }

      // In grouped roles the role text is inside the <li> (often wrapped in one
      // more <a> and a single <div>). Use leaf text elements across the <li>.
      const leaves = leafTextElements(roleLi);
      let dateEl = null;
      let dateIdx = -1;
      for (let i = 0; i < leaves.length; i++) {
        if (dateRe.test(leaves[i].textContent || "")) {
          dateEl = leaves[i];
          dateIdx = i;
          break;
        }
      }
      if (!dateEl) return null;

      let title = "";
      if (dateIdx > 0) {
        // LinkedIn sometimes inserts an employment-type line (e.g. "Full-time")
        // between the real job title and the date line. Skip those leaves.
        let titleIdx = dateIdx - 1;
        while (titleIdx >= 0) {
          const t = fieldText(leaves[titleIdx]);
          if (empTypes.includes(t) || looksLikeDuration(t)) {
            titleIdx--;
          } else {
            break;
          }
        }
        if (titleIdx >= 0) {
          title = fieldText(leaves[titleIdx]);
        }
      }

      let location = "";
      for (let i = dateIdx + 1; i < leaves.length; i++) {
        const t = fieldText(leaves[i]);
        if (!t) continue;
        if (looksLikeLocation(t)) {
          location = stripWorkMode(t);
          break;
        }
        if (t.length > 80 && /[.!?]/.test(t)) break;
      }
      // If the individual role has no location, fall back to the group header.
      if (!location && groupLocation) location = groupLocation;

      const dateText = fieldText(dateEl);
      const text = [title, company, dateText, location].filter(Boolean).join(" ");

      return makeRoleResult(idx, text, roleLi, true, company, title, location);
    }

    function makeRoleResult(idx, text, container, isLi, company, title, location) {
      const normalized = text.replace(/\s+/g, " ").trim();
      const rangeMatch = normalized.match(rangeRe);
      const isCurrent = /\bPresent\b/.test(rangeMatch ? rangeMatch[2] : "");
      const startStr = rangeMatch ? rangeMatch[1] : "";
      const start = parseDate(startStr);
      const result = {
        text: normalized,
        companyFromLink: company,
        titleFromSibling: title,
        locationFromSibling: location,
        isLi,
      };
      return { idx, isCurrent, start, result, container };
    }

    const entries = Array.from(section.querySelectorAll('[componentkey^="entity-collection-item-"]'));
    const roles = [];
    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i];
      const hasUl = !!entry.querySelector(':scope > div > ul, :scope > ul, ul');
      const role = hasUl ? parseGroupedEntry(entry, i) : parseFlatEntry(entry, i);
      if (role) roles.push(role);
    }

    if (!roles.length) return null;

    // Prefer current roles, then latest start date, then earliest DOM order.
    const currentRoles = roles.filter(r => r.isCurrent);
    const pool = currentRoles.length ? currentRoles : roles;
    const picked = pool.reduce((best, cur) => {
      if (!best) return cur;
      const bestDate = best.start || new Date(0);
      const curDate = cur.start || new Date(0);
      if (curDate > bestDate) return cur;
      if (curDate < bestDate) return best;
      return cur.idx < best.idx ? cur : best;
    }, null);

    if (!picked) return null;
    return picked.result;
  };

  // ── Detect Experience DOM shape for diagnostics ──────────────────────────
  const detectExperienceShape = () => {
    const monthRe = /(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4}\s*[-–]/;
    const report = {
      shape: "unknown",
      strategy: "none",
      hasExperienceH2: false,
      directLists: 0,
      directLisWithDateAndCompany: 0,
      datedDivs: 0,
      totalDatedElements: 0,
      sampleTexts: [],
    };

    const main = document.querySelector("main");
    if (!main) return report;

    const expH2 = Array.from(main.querySelectorAll("h2"))
      .find(h => {
        const t = (h.textContent || "").trim();
        return t === "Experience" || t.startsWith("Experience &") || t.startsWith("Experience,");
      });
    report.hasExperienceH2 = !!expH2;
    if (!expH2) return report;

    const section = expH2.closest("section") || expH2.parentElement;
    if (!section) return report;

    const directLists = Array.from(section.querySelectorAll(":scope > ul, :scope > ol"));
    report.directLists = directLists.length;

    let lisWithDateAndCompany = 0;
    for (const list of directLists) {
      const lis = Array.from(list.querySelectorAll(":scope > li"));
      for (const li of lis.slice(0, 3)) {
        const hasDate = monthRe.test(li.textContent || "");
        const hasCompany = !!li.querySelector('a[href*="/company/"]');
        if (hasDate || hasCompany) {
          lisWithDateAndCompany++;
          report.sampleTexts.push({
            tag: "LI",
            class: li.className || "",
            hasDate,
            hasCompany,
            text: (li.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200),
          });
        }
      }
    }
    report.directLisWithDateAndCompany = lisWithDateAndCompany;

    const datedDivs = Array.from(section.querySelectorAll("div, article"))
      .filter(el => monthRe.test(el.textContent || ""));
    report.datedDivs = datedDivs.length;

    if (directLists.length > 0 && lisWithDateAndCompany > 0) {
      report.shape = "list-li";
      report.strategy = "Strategy A: first qualifying <li>";
    } else if (datedDivs.length > 0) {
      report.shape = "div-only";
      report.strategy = "Strategy B: walk up from dated div";
    }

    report.totalDatedElements = lisWithDateAndCompany + datedDivs.length;
    return report;
  };

  // ── Extract the latest experience (only the current role) ─────────────────
  // Per user: just the most recent role, not the full history.
  const extractExperience = () => {
    const shapeReport = detectExperienceShape();
    const item = findLatestExperienceItem();
    if (!item) {
      console.log("[lead-finder/cs] DIAG: no item found", shapeReport);
      return [];
    }

    // The new selector returns structured pieces when it can. Prefer those
    // pieces; only fall back to parseExperienceItem if a piece is missing.
    let title = item.titleFromSibling || "";
    let company = item.companyFromLink || "";
    let location = item.locationFromSibling || "";

    let parsed = null;
    if (!title || !company || !location) {
      parsed = parseExperienceItem(item.text, company || item.companyFromLink, true);
      if (parsed) {
        if (!title) title = parsed.title || "";
        if (!company) company = parsed.company || "";
        if (!location) location = parsed.location || "";
      }
    }

    // Derive date/start/ended/current from the parsed text (most reliable).
    if (!parsed) {
      parsed = parseExperienceItem(item.text, company || item.companyFromLink, true);
    }

    // Safe-mode guard: if the parsed result still contains raw date/location
    // tokens inside the title or company, reject it. This prevents the popup
    // from showing garbage.
    const looksLikeGarbage = (s) => {
      if (!s) return false;
      const t = s.toLowerCase();
      return /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}/.test(t) ||
             /\bpresent\b/.test(t) ||
             /\d+\s*(yrs?|mos?)\b/.test(t);
    };

    const isGarbage = looksLikeGarbage(title) || looksLikeGarbage(company);

    console.log("[lead-finder/cs] DIAG:", {
      BUILD_ID,
      shape: shapeReport.shape,
      strategy: shapeReport.strategy,
      containerTag: item.isLi ? "LI" : "DIV",
      companyFromLink: item.companyFromLink,
      titleFromSibling: item.titleFromSibling,
      locationFromSibling: item.locationFromSibling,
      rawText: item.text,
      parsed,
      safeModeTriggered: isGarbage,
    });

    if (isGarbage) {
      // Return a stub so the user sees the company link and can fill in the
      // rest manually. Do not pollute title/company with raw date blobs.
      return [{
        company: item.companyFromLink || "",
        title: "",
        started_at: parsed ? parsed.started_at : "",
        ended_at: parsed ? parsed.ended_at : "",
        is_current: parsed ? parsed.is_current : false,
        source: "plugin",
        linkedin_slug: extractSlug(window.location.href),
      }];
    }

    if (!title) {
      if (company) {
        return [{
          company,
          title: "",
          started_at: parsed ? parsed.started_at : "",
          ended_at: parsed ? parsed.ended_at : "",
          is_current: parsed ? parsed.is_current : false,
          source: "plugin",
          linkedin_slug: extractSlug(window.location.href),
        }];
      }
      return [];
    }

    return [{
      company,
      title,
      started_at: parsed ? parsed.started_at : "",
      ended_at: parsed ? parsed.ended_at : "",
      is_current: parsed ? parsed.is_current : false,
      location,
      source: "plugin",
      linkedin_slug: extractSlug(window.location.href),
    }];
  };

  // ── Build the debug snapshot payload ─────────────────────────────────────
  const buildDebugPayload = () => {
    const shapeReport = detectExperienceShape();
    const item = findLatestExperienceItem();
    const parsed = item ? parseExperienceItem(item.text, item.companyFromLink, false) : null;
    const company = parsed ? (parsed.company || item.companyFromLink || "") : "";
    const title = parsed ? (parsed.title || "") : "";

    const expH2 = Array.from(document.querySelectorAll("h2"))
      .find(h => {
        const t = (h.textContent || "").trim();
        return t === "Experience" || t.startsWith("Experience &") || t.startsWith("Experience,");
      });
    const section = expH2 ? (expH2.closest("section") || expH2.parentElement) : null;
    const containerHtml = section ? section.outerHTML : "";

    return {
      BUILD_ID,
      linkedin_slug: extractSlug(window.location.href),
      linkedin_url: window.location.href,
      shape: shapeReport,
      raw_text: item ? item.text : "",
      container_html: containerHtml.slice(0, 50000),
      parsed_result: parsed
        ? { company, title, started_at: parsed.started_at, ended_at: parsed.ended_at, is_current: parsed.is_current, location: parsed.location }
        : {},
      safe_mode: false,
    };
  };

  // ── Send the debug snapshot to the server ────────────────────────────────
  const sendDebugSnapshot = () => {
    try {
      const payload = buildDebugPayload();
      chrome.runtime.sendMessage({
        type: "LF_DEBUG_STRUCTURE",
        payload,
      }, (resp) => {
        if (chrome.runtime.lastError) {
          console.warn("[lead-finder/cs] debug snapshot runtime error:", chrome.runtime.lastError.message);
        } else if (!resp || !resp.ok) {
          console.warn("[lead-finder/cs] debug snapshot server error:", resp);
        } else {
          console.log("[lead-finder/cs] debug snapshot saved:", resp.json);
        }
      });
    } catch (e) {
      console.warn("[lead-finder/cs] debug snapshot send failed:", e);
    }
  };

  // ── Parse a single experience role's textContent ─────────────────
  // The text content has no internal separators we can rely on. Observed
  // formats (from real profiles logged in with Premium):
  //
  //   A. Concatenated: "Manager, Technical RecruitingFull-timeJan 2026 - Present · 7 mosHawthorne..."
  //   B. Split-text (siblings): "Mission Manager, Rideshare Program SpaceX (current) Jan 2026 - Present · 7 mos Hawthorne..."
  //   C. 3rd-degree div-only: "Chief Financial Officer Boeing (current) Aug 2025 - Present · 1 yr Seattle..."
  //
  // Algorithm:
  //   1. Normalize whitespace and insert boundaries where LinkedIn has
  //      concatenated words (e.g. "Full-timeJan" → "Full-time Jan").
  //   2. Find the date range.
  //   3. Split title/company from the pre-date text, trusting knownCompany.
  //   4. Extract location from post-duration text.
  const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  const parseExperienceItem = (text, knownCompany = "", debug = false) => {
    if (!text) return null;

    // Normalize whitespace first.
    let cleaned = text.replace(/\s+/g, " ").trim();
    if (!cleaned) return null;

    // Strip LinkedIn's "(current)" label if present.
    cleaned = cleaned.replace(/\s*\(?current\)?\s*/i, " ").trim();

    const monthRe = "(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)";

    // Insert a space between a word and a month/year when they are directly
    // concatenated. This fixes "Full-timeJan 2026" and "RecruitingFull-time".
    cleaned = cleaned.replace(
      new RegExp(`([a-zA-Z.])(\\s*(?:Full-time|Part-time|Self-employed|Contract|Freelance|Internship|Apprenticeship|Temporary|Volunteer))?(\\s*[-·•]\\s*)?(${monthRe}\\s+\\d{4})`, "g"),
      (m, beforeChar, empType, sep, date) => {
        const emp = empType ? ` ${empType}` : "";
        const s = sep ? `${sep.replace(/\s*/g, " ")} `.trim() : "";
        return `${beforeChar}${emp}${s}${date}`.replace(/\s+/g, " ").trim();
      },
    );

    // Also insert space before any standalone month/year that is glued to a
    // preceding lowercase letter (e.g. "ManagerSpaceX Jan 2026" is already
    // handled above, but cover generic cases).
    cleaned = cleaned.replace(
      new RegExp(`([a-z0-9])(${monthRe}\\s+\\d{4})`, "g"),
      "$1 $2",
    );
    cleaned = cleaned.replace(/\s+/g, " ").trim();

    const dateStartRe = new RegExp(
      `(?:^|(?<=\\s)|(?<=[a-z0-9]))((${monthRe}\\s+\\d{4}|\\d{4}))`,
      "i",
    );
    const dateStartMatch = cleaned.match(dateStartRe);
    if (!dateStartMatch) {
      return { title: cleaned, company: knownCompany || "", started_at: "", ended_at: "", is_current: false, location: "" };
    }
    const startIdx = dateStartMatch.index;
    const rest = cleaned.slice(startIdx);
    const rangeRe = new RegExp(
      `^(${monthRe}\\s+\\d{4}|\\d{4})\\s*[-–]\\s*(Present|${monthRe}\\s+\\d{4}|\\d{4})`,
      "i",
    );
    const rangeMatch = rest.match(rangeRe);
    if (!rangeMatch) {
      return { title: cleaned, company: knownCompany || "", started_at: "", ended_at: "", is_current: false, location: "" };
    }
    const endIdx = startIdx + rangeMatch[0].length;

    // The text BEFORE the date is: title + (optional company) + (optional
    // employment type). Split it.
    const before = cleaned.slice(0, startIdx).trim();
    // After the date is: duration + (optional location) + (optional description)
    const afterDate = cleaned.slice(endIdx).trim();

    // Strip employment type from end of before-date text
    const empTypeRe = /\s*[-]?\s*(Full-time|Part-time|Self-employed|Contract|Freelance|Internship|Apprenticeship|Temporary|Volunteer)\s*$/i;
    let beforeClean = before.replace(empTypeRe, "").trim();
    // Also strip trailing bullet separators (· or •)
    beforeClean = beforeClean.replace(/\s*[·•]\s*$/, "").trim();

    // Company / title split.
    // If the caller already found a /company/ link, trust that company and
    // use the entire pre-date text as the title. Strip the known company even
    // when concatenated (with or without a space).
    let title = beforeClean;
    let company = knownCompany || "";
    if (company) {
      const re = new RegExp(escapeRegExp(company) + "\\s*$", "i");
      title = beforeClean.replace(re, "").trim();
      // If the company wasn't at the end, try stripping it with a space before
      // it ("SpaceX Jan 2026" would not reach here, but "Mission Manager, Rideshare Program SpaceX" would).
      if (title === beforeClean) {
        const re2 = new RegExp("\\s+" + escapeRegExp(company) + "\\s*$", "i");
        title = beforeClean.replace(re2, "").trim();
      }
    }

    // Fallback splitting only when we don't already have a company.
    if (!company) {
      // Try a camelCase boundary (e.g. "ProgramSpaceX" → "Program" + "SpaceX").
      const camelMatch = beforeClean.match(/^(.+?)([a-z])([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*)*)$/);
      if (camelMatch) {
        title = camelMatch[1] + camelMatch[2];
        company = camelMatch[3].trim();
      } else {
        // Multi-word capitalized company name at the end.
        const companyRe = /^(.+?)\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*)*(?:\s+(?:Inc|LLC|Ltd|Corp|Corporation|Company|Co|Group|Holdings|Industries|Technologies|Systems|Solutions|Partners|LP|GmbH|AG|S\.A\.|S\.r\.l\.|B\.V\.|Pty)\.?)?)\s*$/;
        const companyMatch = beforeClean.match(companyRe);
        if (companyMatch) {
          const candidate = companyMatch[2];
          const beforeCandidate = companyMatch[1].trim();
          const looksLikeCompany =
            candidate.length >= 3 &&
            candidate.length <= 100 &&
            beforeCandidate.length >= 2 &&
            /^[A-Z]/.test(candidate) &&
            (/\s/.test(candidate) ||
             /\b(Inc|LLC|Ltd|Corp|Co|Group|Holdings|Industries|Technologies|Systems|Solutions|Partners|Company)\b/i.test(candidate));
          if (looksLikeCompany) {
            title = beforeCandidate;
            company = candidate;
          }
        }
      }
    }

    // Location extraction. Strip duration token, then take the first
    // location-looking chunk before a work-mode bullet or description sentence.
    let after = afterDate.replace(
      /^(?:[·•]\s*)?\d+\s*(?:yrs?|mos?)\s*(?:\d+\s*mos?\s*)?/,
      "",
    ).trim();

    let location = "";
    if (after) {
      // Work-mode bullets separate location from the rest.
      const bulletIdx = after.indexOf("·");
      const beforeBullet = bulletIdx >= 0 ? after.slice(0, bulletIdx).trim() : after;
      // Description sentences start with "X. Capital". Stop before them.
      const descMatch = beforeBullet.match(/^(.+?)\.\s+[A-Z]/);
      let candidate = descMatch ? descMatch[1].trim() : beforeBullet;
      // Some DOMs concatenate work mode directly after the location.
      candidate = candidate.replace(/\s*(On-site|Remote|Hybrid)\s*$/i, "").trim();
      // Reject anything that still contains a date or duration.
      if (candidate && !/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}\b/i.test(candidate) && !/\b\d+\s*(yrs?|mos?)\b/i.test(candidate)) {
        if (
          /,\s*[A-Z]/.test(candidate) ||
          /Metropolitan|Greater|Bay Area|County/i.test(candidate) ||
          (/^[A-Z][a-z]+(?:\s[A-Z][a-z]+)*$/.test(candidate) && candidate.length < 60)
        ) {
          location = candidate;
        }
      }
    }
    const parts = rangeMatch[0].split(/\s*[-–]\s*/);
    const result = {
      title,
      company,
      started_at: parts[0] || "",
      ended_at: parts[1] === "Present" ? "" : (parts[1] || ""),
      is_current: parts[1] === "Present",
      location,
    };
    if (debug) {
      console.log("[lead-finder/cs] parseExperienceItem debug:", {
        raw: cleaned,
        before,
        beforeClean,
        knownCompany,
        afterDate,
        after,
        candidate: location ? location : "(none)",
        result,
      });
    }
    return result;
  };

  // ── Build the structured payload ──────────────────────────────────────────
  let lastExtracted = null;
  const extract = () => {
    const linkedin_url = window.location.href;
    const linkedin_slug = extractSlug(linkedin_url);

    // Real DOM extraction (the user is logged in with Premium and sees
    // everything they have access to). Strategy: get the experience list
    // first, use the first item as the source of truth for current role,
    // and use the top card for the name + location.
    const experience = extractExperience();
    const full_name = extractName();
    // Use company and location ONLY from the current role. The top card is
    // unreliable for both: it can show a personal location, and it lacks the
    // company when the current role's company link lives inside the Experience
    // section. If the role text has no company/location, leave them blank so
    // the user can fill them in manually.
    let location = "";
    if (experience.length > 0 && experience[0].location) {
      location = experience[0].location;
    }
    let current_company = "";
    if (experience.length > 0) {
      current_company = experience[0].company || "";
    }
    log(`Company/location from experience: company=${current_company || "(none)"}, location=${location || "(none)"}`);
    // Title: only the experience list has it.
    let current_title = "";
    if (experience.length > 0) {
      current_title = experience[0].title;
    }

    log("Name:", full_name);
    log("Title:", current_title);
    log("Company:", current_company);
    log("Location:", location);
    log(`Experience: ${experience.length} role(s)`);

    // Derive first/last name
    const nameParts = (full_name || "").split(/\s+/).filter(Boolean);
    const first_name = nameParts[0] || "";
    const last_name = nameParts.slice(1).join(" ");

    // ALWAYS return a payload, even if extraction was partial. The user can
    // always fill the form fields manually in the popup.
    const payload = {
      linkedin_url,
      linkedin_slug,
      full_name,
      first_name,
      last_name,
      current_title,
      current_company,
      location,
      email: null,
      phone: null,
      experience,
    };
    lastExtracted = payload;
    return payload;
  };

  // ── Highlight-to-extract mode (Phase 3 / t3.1) ────────────────────────────
  // If the user selects text on the page, show a small floating button.
  // On click, capture the selected text + the nearest field-name context.
  let highlightBtn = null;
  const showHighlightButton = (x, y) => {
    if (!highlightBtn) {
      highlightBtn = document.createElement("div");
      highlightBtn.id = "lead-finder-highlight-btn";
      highlightBtn.textContent = "Extract to Lead Finder";
      document.body.appendChild(highlightBtn);
    }
    highlightBtn.style.left = `${x}px`;
    highlightBtn.style.top = `${y}px`;
    highlightBtn.style.display = "block";
  };
  const hideHighlightButton = () => {
    if (highlightBtn) highlightBtn.style.display = "none";
  };
  document.addEventListener("mouseup", (ev) => {
    const sel = window.getSelection();
    const text = sel ? sel.toString().trim() : "";
    if (text.length < 2) {
      hideHighlightButton();
      return;
    }
    showHighlightButton(ev.pageX, ev.pageY);
  });
  document.addEventListener("mousedown", () => hideHighlightButton());
  document.addEventListener("click", (ev) => {
    if (ev.target && ev.target.id === "lead-finder-highlight-btn") {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : "";
      if (text) {
        // Find the nearest field name. Walk up to find a labelled element.
        let anchor = ev.target;
        let label = "";
        for (let i = 0; i < 6 && anchor; i++) {
          // Look for a nearby <dt>, <label>, or a sibling with a known field name.
          const dt = anchor.closest("section, div");
          if (dt) {
            const heading = dt.querySelector("h1, h2, h3, h4, dt, label, [aria-label]");
            if (heading && heading.textContent && heading !== anchor) {
              label = heading.textContent.trim().slice(0, 80);
              break;
            }
          }
          anchor = anchor.parentElement || null;
        }
        chrome.runtime.sendMessage({
          type: "LF_HIGHLIGHT_EXTRACT",
          text,
          context_label: label,
          page_url: window.location.href,
        });
      }
      hideHighlightButton();
    }
  });

  // ── Message handler: respond to popup's "give me the data" request ─────────
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.type) return false;
    if (msg.type === "LF_EXTRACT") {
      const data = lastExtracted || extract();
      // Send a debug snapshot now that the service worker is awake (the popup
      // triggered this message). This is more reliable than the initial page-load
      // snapshot, which could fire before the worker was ready.
      sendDebugSnapshot();
      sendResponse({ ok: true, data });
      return false; // synchronous
    }
    if (msg.type === "LF_RE_EXTRACT") {
      // Force a fresh extraction (e.g. the user clicked "re-scan").
      const data = extract();
      sendDebugSnapshot();
      sendResponse({ ok: !!data, data });
      return false;
    }
    if (msg.type === "LF_SEND_DEBUG") {
      // Manual debug snapshot requested by the popup.
      sendDebugSnapshot();
      sendResponse({ ok: true });
      return false;
    }
    if (msg.type === "LF_PING") {
      sendResponse({ ok: true, on_profile: !!lastExtracted });
      return false;
    }
    return false;
  });

  // ── Debounced MutationObserver for re-extraction ───────────────────────────
  let debounceTimer = null;
  const observer = new MutationObserver(() => {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      try {
        const fresh = extract();
        if (fresh) {
          chrome.runtime.sendMessage({ type: "LF_DATA_UPDATED", data: fresh }).catch(() => {});
        }
      } catch (e) {
        warn("Re-extract failed:", e);
      }
    }, 500);
  });
  observer.observe(document.body, { childList: true, subtree: true });

  // ── Initial extraction ─────────────────────────────────────────────────────
  // Wait for the page to settle. document_idle already gives us most of the
  // DOM, but LinkedIn lazy-loads the experience section.
  const tryInitialExtract = (attempt = 0) => {
    const data = extract();
    if (!data) return;
    const haveName = !!data.full_name;
    const haveCompany = !!data.current_company;
    const expCount = (data.experience || []).length;
    if (haveName && haveCompany && expCount > 0) {
      log(`Initial extract OK: name=${data.full_name} company=${data.current_company} roles=${expCount}`);
    } else if (attempt < 8) {
      // Partial extract — try again after the page settles
      setTimeout(() => tryInitialExtract(attempt + 1), 500);
    } else {
      warn(
        `Initial extract partial: name=${haveName} company=${haveCompany} roles=${expCount}. ` +
        `The user can still fill the form manually. Try the highlight-to-extract mode: ` +
        `select the name/company with the mouse to capture them.`,
      );
    }
  };
  tryInitialExtract();
})();
