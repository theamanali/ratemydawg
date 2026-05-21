import type { PlasmoCSConfig } from "plasmo"

export const config: PlasmoCSConfig = {
  matches: ["https://myplan.uw.edu/course/*"],
}

const API_BASE = process.env.PLASMO_PUBLIC_API_BASE!

const cache: Record<string, Professor[]> = {}

const TOOLTIPS: Record<string, { title: string; desc: string }> = {
  QR: {
    title: "Quality Rating (RMP)",
    desc: "Average quality rating from RateMyProfessors student reviews, on a scale of 1–5. Higher is better.",
  },
  DR: {
    title: "Difficulty Rating (RMP)",
    desc: "Average difficulty rating from RateMyProfessors student reviews, on a scale of 1–5. Higher means more difficult.",
  },
  WTA: {
    title: "Would Take Again (RMP)",
    desc: "Percentage of RateMyProfessors reviewers who said they would take this professor again.",
  },
  CES: {
    title: "Course Evaluation Score",
    desc: "Weighted average of median responses across all UW course evaluation questions per section, weighted by students surveyed. Scale of 0–5. Higher is better.",
  },
}

function ratingColor(value: number, reverse = false, min = 1, max = 5): string {
  const t = Math.max(0, Math.min(1, reverse ? 1 - (value - min) / (max - min) : (value - min) / (max - min)))
  const r = Math.round(t < 0.5 ? 255 : 255 * (1 - t) * 2)
  const g = Math.round(t > 0.5 ? 255 : 255 * t * 2)
  const b = Math.round(20 * Math.sin(Math.PI * t))
  return `rgb(${r}, ${g}, ${b})`
}

function createTooltip() {
  const existing = document.getElementById("rmd-tooltip")
  if (existing) return existing

  const style = document.createElement("style")
  style.textContent = `
    #rmd-tooltip {
      position: fixed;
      background: #fff;
      color: rgb(33, 37, 41);
      border: 1px solid rgba(0,0,0,0.12);
      border-top: 2.5px solid var(--rmd-tip-color, rgba(100,180,100,0.8));
      border-radius: 7px;
      padding: 10px 13px;
      font-size: 1rem;
      font-weight: 400;
      font-family: 'Open Sans', sans-serif;
      max-width: 268px;
      pointer-events: none;
      z-index: 99999;
      box-shadow: 0 6px 18px rgba(0,0,0,0.13), 0 1px 5px rgba(0,0,0,0.07);
      line-height: 1.5;
      opacity: 0;
      visibility: hidden;
      transform: translateY(3px);
      transition: opacity 0.13s ease, transform 0.13s ease, visibility 0s linear 0.13s;
    }
    #rmd-tooltip.rmd-tip-on {
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
      transition: opacity 0.13s ease, transform 0.13s ease;
    }
    #rmd-tooltip strong {
      display: block;
      font-size: 1.125rem;
      font-weight: 600;
      margin-bottom: 4px;
      color: rgb(33, 37, 41);
    }
    #rmd-tooltip::before, #rmd-tooltip::after {
      content: '';
      position: absolute;
      left: var(--rmd-arrow-x, 50%);
      transform: translateX(-50%);
      pointer-events: none;
      width: 0;
      height: 0;
    }
    #rmd-tooltip::before {
      top: -7px;
      border: 6px solid transparent;
      border-top-width: 0;
      border-bottom-color: var(--rmd-tip-color, rgba(100,180,100,0.8));
    }
    #rmd-tooltip::after {
      top: -5px;
      border: 5px solid transparent;
      border-top-width: 0;
      border-bottom-color: #fff;
    }
    #rmd-tooltip.rmd-tip-above::before {
      top: auto;
      bottom: -7px;
      border-top-width: 6px;
      border-bottom-width: 0;
      border-top-color: rgba(0,0,0,0.15);
      border-bottom-color: transparent;
    }
    #rmd-tooltip.rmd-tip-above::after {
      top: auto;
      bottom: -5px;
      border-top-width: 5px;
      border-bottom-width: 0;
      border-top-color: #fff;
      border-bottom-color: transparent;
    }
  `
  document.head.appendChild(style)

  const tooltip = document.createElement("div")
  tooltip.id = "rmd-tooltip"
  document.body.appendChild(tooltip)

  document.addEventListener("mouseover", (e) => {
    const pill = (e.target as Element).closest("[data-rmd-key]")
    if (!pill) return
    const key = pill.getAttribute("data-rmd-key")
    const info = TOOLTIPS[key]
    if (!info) return
    const statRaw = pill.getAttribute("data-rmd-stat")

    const pillBox = (pill as HTMLElement).querySelector("span:last-child") as HTMLElement | null
    if (pillBox?.style.background) tooltip.style.setProperty("--rmd-tip-color", pillBox.style.background)

    tooltip.textContent = ""

    const titleEl = document.createElement("strong")
    titleEl.textContent = info.title
    tooltip.appendChild(titleEl)

    if (statRaw) {
      const { text, color } = JSON.parse(statRaw)
      const [num, ...rest] = text.split(" ")

      const statEl = document.createElement("span")
      statEl.style.cssText = "display:block; color:rgb(33,37,41); margin-bottom:4px; font-size:1rem; font-family:'Open Sans',sans-serif;"

      const numEl = document.createElement("span")
      numEl.style.cssText = `background:${color}; border-radius:4px; padding:1px 5px; font-weight:600; color:rgb(33,37,41); margin-right:3px;`
      numEl.textContent = num
      statEl.appendChild(numEl)

      const restEl = document.createElement("span")
      // stat text contains intentional <b> tags wrapping numbers (e.g. "from <b>42</b> reviews")
      rest.join(" ").split(/(<b>[^<]*<\/b>)/).forEach((part, i) => {
        if (i % 2 === 1) {
          const b = document.createElement("b")
          b.textContent = part.slice(3, -4)
          restEl.appendChild(b)
        } else if (part) {
          restEl.appendChild(document.createTextNode(part))
        }
      })
      statEl.appendChild(restEl)
      tooltip.appendChild(statEl)

      const divider = document.createElement("span")
      divider.style.cssText = "display:block; border-top:1px solid rgba(0,0,0,0.08); margin:6px 0 5px;"
      tooltip.appendChild(divider)
    }

    const descEl = document.createElement("span")
    descEl.style.cssText = "display:block; color:rgb(90,90,90); font-size:0.875rem; font-family:'Open Sans',sans-serif;"
    descEl.textContent = info.desc
    tooltip.appendChild(descEl)

    // Anchor to the pill: center below, flip above if insufficient room, clamp to viewport
    const rect = (pillBox ?? pill as HTMLElement).getBoundingClientRect()
    tooltip.style.left = "0px"  // reset before measuring — fixed elements' width is constrained by (viewport_width - left)
    const tw = tooltip.offsetWidth
    const th = tooltip.offsetHeight
    const gap = 7
    const margin = 8
    let x = rect.left + rect.width / 2 - tw / 2
    let y = rect.bottom + gap
    const isAbove = y + th > window.innerHeight - margin
    if (isAbove) y = rect.top - th - gap
    x = Math.max(margin, Math.min(x, window.innerWidth - tw - margin))
    y = Math.max(margin, y)
    // Arrow x offset: pill center relative to tooltip left, clamped inside tooltip
    const arrowX = Math.max(12, Math.min(tw - 12, rect.left + rect.width / 2 - x))
    tooltip.style.setProperty("--rmd-arrow-x", arrowX + "px")
    tooltip.style.left = x + "px"
    tooltip.style.top  = y + "px"
    tooltip.classList.toggle("rmd-tip-above", isAbove)
    tooltip.classList.add("rmd-tip-on")
  })

  document.addEventListener("mouseout", (e) => {
    const leaving = (e.target as Element).closest("[data-rmd-key]")
    const entering = (e.relatedTarget as Element)?.closest("[data-rmd-key]")
    if (leaving && leaving !== entering) tooltip.classList.remove("rmd-tip-on")
  })

  return tooltip
}

function pill(
  key: string,
  value: string | null,
  color: string | null,
  animate = true,
  statLine: string | null = null
): HTMLElement {
  const bg = color ?? "rgb(180,180,180)"
  const text = value ?? "?"

  const wrapper = document.createElement("span")
  wrapper.setAttribute("data-rmd-key", key)
  if (statLine) wrapper.setAttribute("data-rmd-stat", JSON.stringify({ text: statLine, color: bg }))
  wrapper.style.cssText =
    "display:inline-flex; align-items:center; gap:2px; font-size:0.875rem; font-family:'Open Sans',sans-serif; white-space:nowrap; cursor:default;"

  const label = document.createElement("span")
  label.style.cssText = "color:rgb(90,90,90); font-weight:500;"
  label.textContent = key

  const box = document.createElement("span")
  const isNA = bg === "rgb(180,180,180)"
  const shouldAnimate = animate && !isNA
  const naStyles = isNA ? " border:1.5px dashed rgb(180,180,180); background:transparent; color:rgb(160,160,160);" : ""
  box.style.cssText = `background:${shouldAnimate ? "rgb(255,0,20)" : (isNA ? "transparent" : bg)}; border-radius:4px; padding:2px 5px; color:${isNA ? "rgb(160,160,160)" : "rgb(33,37,41)"}; font-weight:600; display:inline-block; line-height:1;${naStyles}${shouldAnimate ? " transition:background 0.6s ease;" : ""}`
  box.textContent = shouldAnimate ? (text.endsWith("%") ? "0%" : "0.0") : text

  if (shouldAnimate) {
    const isPercent = text.endsWith("%")
    const finalNum = parseFloat(text)
    const duration = 600
    const start = performance.now()

    requestAnimationFrame(function tick(now) {
      const progress = Math.min((now - start) / duration, 1)
      const current = finalNum * progress
      box.textContent = isPercent ? `${Math.round(current)}%` : current.toFixed(1)
      if (progress < 1) requestAnimationFrame(tick)
      else box.textContent = text
    })

    requestAnimationFrame(() => {
      requestAnimationFrame(() => { box.style.background = bg })
    })
  }

  wrapper.appendChild(label)
  wrapper.appendChild(box)
  return wrapper
}

interface Professor {
  avg_quality_rating: number | null
  avg_difficulty_rating: number | null
  would_take_again_percent: number | null
  avg_eval_median_weighted: number | null
  rmp_rating_count: number | null
  cec_surveyed_count: number | null
  cec_eval_count: number | null
  cec_locked?: boolean
}

function injectBadge(el: HTMLElement, prof: Professor, animate = true) {
  if (el.querySelector(".rmd-badge")) return

  const badge = document.createElement("div")
  badge.className = "rmd-badge"
  badge.style.cssText =
    "display:flex; gap:4px; align-items:center; flex-wrap:nowrap; margin-top:0; opacity:0; transition:opacity 0.1s ease;"
  requestAnimationFrame(() => {
    requestAnimationFrame(() => { badge.style.opacity = "1" })
  })

  const { avg_quality_rating: qr, avg_difficulty_rating: dr, would_take_again_percent: wta, avg_eval_median_weighted: ces, rmp_rating_count: rmpCount, cec_surveyed_count: cecCount, cec_eval_count: cecEvals } = prof

  const rmpSuffix = rmpCount ? ` from <b>${rmpCount}</b> reviews` : ""
  const cecSuffix = cecCount && cecEvals ? ` calculated from <b>${cecCount}</b> surveys across <b>${cecEvals}</b> sections` : ""

  badge.appendChild(pill("QR", qr != null ? qr.toFixed(1) : null, qr != null ? ratingColor(qr) : null, animate, qr != null ? `${qr.toFixed(1)} calculated${rmpSuffix}` : null))
  badge.appendChild(pill("DR", dr != null ? dr.toFixed(1) : null, dr != null ? ratingColor(dr, true) : null, animate, dr != null ? `${dr.toFixed(1)} calculated${rmpSuffix}` : null))
  badge.appendChild(pill("WTA", wta != null ? `${wta.toFixed(0)}%` : null, wta != null ? ratingColor(wta, false, 0, 100) : null, animate, wta != null ? `${wta.toFixed(0)}% of <b>${rmpCount}</b> reviewers would take again` : null))

  const sep = document.createElement("span")
  sep.style.cssText = "width:1px; height:1em; background:rgba(0,0,0,0.12); border-radius:1px; margin:0 1px; flex-shrink:0; align-self:center;"
  badge.appendChild(sep)

  if (prof.cec_locked) {
    const lockedPill = pill("CES", null, null, false, null)
    const lockBox = lockedPill.lastElementChild as HTMLElement
    lockBox.style.cssText = "background:rgb(235,235,235); border-radius:4px; padding:2px 5px; display:inline-flex; align-items:center; justify-content:center; width:22px; height:18px;"
    const svgNS = "http://www.w3.org/2000/svg"
    const svg = document.createElementNS(svgNS, "svg")
    svg.setAttribute("width", "9")
    svg.setAttribute("height", "10")
    svg.setAttribute("viewBox", "0 0 9 10")
    svg.setAttribute("fill", "none")
    const rect = document.createElementNS(svgNS, "rect")
    rect.setAttribute("x", "0.75"); rect.setAttribute("y", "4")
    rect.setAttribute("width", "7.5"); rect.setAttribute("height", "5.75")
    rect.setAttribute("rx", "1.25"); rect.setAttribute("fill", "rgb(120,120,120)")
    const path = document.createElementNS(svgNS, "path")
    path.setAttribute("d", "M2 4V2.75a2.5 2.5 0 0 1 5 0V4")
    path.setAttribute("stroke", "rgb(120,120,120)")
    path.setAttribute("stroke-width", "1.5"); path.setAttribute("stroke-linecap", "round")
    svg.appendChild(rect); svg.appendChild(path)
    lockBox.appendChild(svg)
    lockedPill.style.cursor = "pointer"
    lockedPill.title = "Sign in with your UW account to see CEC scores"
    lockedPill.addEventListener("click", () => chrome.runtime.sendMessage({ type: "OPEN_POPUP" }))
    badge.appendChild(lockedPill)
  } else {
    badge.appendChild(pill("CES", ces != null ? ces.toFixed(1) : null, ces != null ? ratingColor(ces, false, 0, 5) : null, animate, ces != null ? `${ces.toFixed(1)}${cecSuffix}` : null))
  }

  el.appendChild(badge)
}

async function matchAndInject() {
  // Handle both single instructor (div.mb-1) and multiple instructors (ul.mb-1 > li)
  const instructorEls: HTMLElement[] = []
  document.querySelectorAll<HTMLElement>(".cdpSectionsTable .mb-1").forEach((el) => {
    if (el.tagName === "UL") {
      el.querySelectorAll<HTMLElement>("li").forEach((li) => {
        if (li.textContent?.trim()) instructorEls.push(li)
      })
    } else if (el.textContent?.trim()) {
      instructorEls.push(el)
    }
  })

  if (!instructorEls.length) return

  const names = [...new Set(instructorEls.map((el) => el.textContent!.trim()))]
  const uncached = names.filter((n) => !(n in cache))

  if (uncached.length) {
    try {
      const { jwt } = await chrome.storage.local.get("jwt")
      const headers: Record<string, string> = { "Content-Type": "application/json" }
      if (jwt) headers["Authorization"] = `Bearer ${jwt}`

      const res = await fetch(`${API_BASE}/professors/match/batch`, {
        method: "POST",
        headers,
        body: JSON.stringify({ names: uncached }),
      })
      if (res.ok) {
        const data = await res.json()
        Object.assign(cache, data)
      }
    } catch {}
  }

  createTooltip()

  let animateCount = 0
  const animatedNames = new Set<string>()

  instructorEls.forEach((el) => {
    const name = el.textContent!.trim()
    const matches = cache[name]
    if (!matches?.length) return
    const alreadyInjected = !!el.querySelector(".rmd-badge")
    if (alreadyInjected) { injectBadge(el, matches[0], false); return }
    const shouldAnimate = !animatedNames.has(name) && animateCount < 2
    if (shouldAnimate) { animatedNames.add(name); animateCount++ }
    injectBadge(el, matches[0], shouldAnimate)
  })
}

function waitForTable(callback: () => void) {
  if (document.querySelector(".cdpSectionsTable")) {
    callback()
    return
  }
  const observer = new MutationObserver(() => {
    if (document.querySelector(".cdpSectionsTable")) {
      observer.disconnect()
      callback()
    }
  })
  observer.observe(document.body, { childList: true, subtree: true })
}

// Run when table appears, re-run on URL changes (MyPlan is a SPA)
waitForTable(matchAndInject)

// Refresh when JWT changes (sign-in or sign-out)
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "jwt" in changes) {
    Object.keys(cache).forEach(k => delete cache[k])
    document.querySelectorAll(".rmd-badge").forEach(el => el.remove())
    matchAndInject()
  }
})

// Listen for refresh message as fallback
chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "REFRESH") {
    Object.keys(cache).forEach(k => delete cache[k])
    document.querySelectorAll(".rmd-badge").forEach(el => el.remove())
    matchAndInject()
  }
})

let debounceTimer: ReturnType<typeof setTimeout> | null = null

function onUrlChange() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => waitForTable(matchAndInject), 500)
}

// Patch history.pushState to catch SPA navigations (popstate doesn't fire for pushState)
const _origPushState = history.pushState.bind(history)
history.pushState = function (...args) {
  _origPushState(...args)
  onUrlChange()
}
window.addEventListener("popstate", onUrlChange)
