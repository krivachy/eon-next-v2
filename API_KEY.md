# Creating an E.ON Next API key

E.ON Next moved customer login to Auth0 with a CAPTCHA in October 2026, and its
API no longer accepts an email and password from integrations. From version
`2.0.0` this integration signs in with a per-account **API key** instead. The key
looks like `sk_live_...`.

> [!WARNING]
> The API key gives **full access to your E.ON Next account**. Treat it like a
> password: store it in a password manager, never share it, and never paste it
> into issues, forum posts or logs. The integration never logs it.

## Steps

1. Log in to your account at [eonnext.com](https://www.eonnext.com) in a desktop
   browser.
2. Open the browser's developer tools (<kbd>F12</kbd>, or
   <kbd>Cmd</kbd>+<kbd>Option</kbd>+<kbd>I</kbd> on macOS) and go to the
   **Console** tab. Some browsers ask you to type `allow pasting` before they
   accept pasted code.
3. Paste the script below and press <kbd>Enter</kbd>.
4. Copy the printed key (`sk_live_...`) into your password manager, then into the
   **API key** field when you add the integration in Home Assistant. E.ON shows
   the full key **only once**; afterwards it only ever returns a masked version.

```js
(async () => {
  const url = 'https://api.eonnext-kraken.energy/v1/graphql/';
  // The signed-in session keeps an Auth0 id_token in localStorage; depending on
  // the cache entry it sits at the top level or under `body`.
  const pick = (c) => c?.id_token || c?.decodedToken?.raw || c?.body?.id_token || c?.body?.decodedToken?.raw;
  let idToken = null;
  for (const k of Object.keys(localStorage)) {
    if (!k.startsWith('@@auth0spajs@@')) continue;
    try { idToken = pick(JSON.parse(localStorage[k])); } catch {}
    if (idToken) break;
  }
  if (!idToken) return console.log('RESULT: not signed in (no Auth0 id_token found). Log in at eonnext.com, reload the page and try again.');
  const gql = (query) =>
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', authorization: 'Bearer ' + idToken },
      body: JSON.stringify({ query }),
    }).then((r) => r.json());

  // Never regenerate an existing key here: that would invalidate it everywhere it is used.
  const check = await gql('query { viewer { liveSecretKey } }');
  const viewer = check?.data?.viewer;
  if (check?.errors || !viewer) return console.log('RESULT: E.ON did not accept the session:', JSON.stringify(check?.errors ?? check));
  if (viewer.liveSecretKey) {
    return console.log('RESULT: this account already has an API key, so no new key was created. E.ON only shows it masked (' + viewer.liveSecretKey.slice(0, 12) + '...). Use the key you saved when it was created. If you no longer have it, see "Lost your key?" in API_KEY.md.');
  }

  const created = await gql('mutation { regenerateSecretKey { key } }');
  console.log('RESULT:', created?.data?.regenerateSecretKey?.key ?? JSON.stringify(created?.errors ?? created));
})();
```

## What the script does

- Reads the Auth0 `id_token` that eonnext.com keeps in `localStorage` (keys that
  start with `@@auth0spajs@@`) after you log in. Nothing is sent anywhere except
  E.ON's own API.
- Asks E.ON whether your account already has a key (`viewer { liveSecretKey }`).
  - If one exists, it says so and **stops**. It does not create a new key,
    because regenerating would invalidate the old one and break anything already
    using it. E.ON returns the existing key masked, so the full value can't be
    recovered from here.
  - If none exists, it creates one with `mutation { regenerateSecretKey { key } }`
    and prints it.

## Lost your key?

E.ON allows one key per account and never shows the full key again after it is
created. If the script above says a key exists and you don't have it saved, the
only option is to **regenerate** it. The new key replaces the old one
immediately: anything else still using the old key (another integration,
Predbat, a script) stops working until you give it the new key.

Run this in the same logged-in eonnext.com console. It asks for confirmation
first:

```js
(async () => {
  const pick = (c) => c?.id_token || c?.decodedToken?.raw || c?.body?.id_token || c?.body?.decodedToken?.raw;
  let idToken = null;
  for (const k of Object.keys(localStorage)) {
    if (!k.startsWith('@@auth0spajs@@')) continue;
    try { idToken = pick(JSON.parse(localStorage[k])); } catch {}
    if (idToken) break;
  }
  if (!idToken) return console.log('RESULT: not signed in. Log in at eonnext.com, reload the page and try again.');
  if (!confirm('Regenerate your E.ON Next API key? The old key stops working immediately everywhere it is used.')) return console.log('RESULT: cancelled, nothing changed.');
  const r = await fetch('https://api.eonnext-kraken.energy/v1/graphql/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', authorization: 'Bearer ' + idToken },
    body: JSON.stringify({ query: 'mutation { regenerateSecretKey { key } }' }),
  }).then((x) => x.json());
  console.log('RESULT:', r?.data?.regenerateSecretKey?.key ?? JSON.stringify(r?.errors ?? r));
})();
```

Save the new key in your password manager, then re-authenticate the integration
with it (Home Assistant prompts for this once the old key is rejected).

## Re-authentication and rotation

- The key is long-lived. When Home Assistant asks you to re-authenticate (for
  example after the refresh token expires), you can usually leave the field blank
  to reuse the saved key, or paste the same key again.
- If you think the key has leaked, regenerate it as described in
  [Lost your key?](#lost-your-key) and re-authenticate with the new key.

## Credits

This approach and script are based on the API-key work in
[TobySmithRM/eon-next-v2](https://github.com/TobySmithRM/eon-next-v2) (MIT).
