const CLIENT_ID = "30bc3163-76bd-4994-a153-afdfb9cf3b4c"
const TENANT_ID = "uw.edu"
const API_BASE = "https://api.ratemydawg.com"

async function signIn() {
  const redirectUri = `https://${chrome.runtime.id}.chromiumapp.org/`
  const authUrl =
    `https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/authorize` +
    `?client_id=${CLIENT_ID}` +
    `&response_type=code` +
    `&redirect_uri=${encodeURIComponent(redirectUri)}` +
    `&scope=${encodeURIComponent("openid email profile")}` +
    `&response_mode=query` +
    `&prompt=select_account` +
    `&domain_hint=uw.edu`

  return new Promise<string | null>((resolve) => {
    chrome.identity.launchWebAuthFlow(
      { url: authUrl, interactive: true },
      async (redirectUrl) => {
        if (chrome.runtime.lastError || !redirectUrl) {
          resolve(null)
          return
        }

        const url = new URL(redirectUrl)
        const code = url.searchParams.get("code")
        if (!code) { resolve(null); return }

        try {
          const res = await fetch(`${API_BASE}/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code, redirect_uri: redirectUri }),
          })
          if (!res.ok) { resolve(null); return }
          const { token } = await res.json()
          await chrome.storage.local.set({ jwt: token })
          chrome.tabs.query({}, (tabs) => {
            tabs.filter(t => t.url?.includes("myplan.uw.edu")).forEach(tab => {
              chrome.tabs.sendMessage(tab.id!, { type: "REFRESH" }).catch(() => {})
            })
          })
          resolve(token)
        } catch {
          resolve(null)
        }
      }
    )
  })
}

async function signOut() {
  await chrome.storage.local.remove("jwt")
}

async function getJwt(): Promise<string | null> {
  const { jwt } = await chrome.storage.local.get("jwt")
  return jwt ?? null
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "SIGN_IN") {
    signIn().then(sendResponse)
    return true
  }
  if (message.type === "SIGN_OUT") {
    signOut().then(() => sendResponse(null))
    return true
  }
  if (message.type === "GET_JWT") {
    getJwt().then(sendResponse)
    return true
  }
  if (message.type === "OPEN_POPUP") {
    chrome.action.openPopup()
    return false
  }
  if (message.type === "REFRESH_TABS") {
    chrome.tabs.query({}, (tabs) => {
      tabs.forEach(tab => {
        if (tab.url?.includes("myplan.uw.edu")) {
          chrome.tabs.sendMessage(tab.id!, { type: "REFRESH" }).catch(() => {})
        }
      })
    })
    return false
  }
})
