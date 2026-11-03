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
   **API key** field when you add the integration in Home Assistant.

```js
(async () => {
  const url = 'https://api.eonnext-kraken.energy/v1/graphql/';
  // The signed-in session keeps an Auth0 id_token in localStorage.
  const entry = Object.keys(localStorage).find((k) => {
    if (!k.startsWith('@@auth0spajs@@')) return false;
    try { return !!JSON.parse(localStorage[k]).id_token; } catch { return false; }
  });
  if (!entry) return console.log('RESULT: not signed in (no Auth0 id_token found). Log in at eonnext.com and try again.');
  const idToken = JSON.parse(localStorage[entry]).id_token;
  const gql = (query) =>
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', authorization: 'Bearer ' + idToken },
      body: JSON.stringify({ query }),
    }).then((r) => r.json());

  // Never regenerate an existing key: that would invalidate it everywhere it is used.
  const check = await gql('query { viewer { liveSecretKey } }');
  if (check.errors) return console.log('RESULT: could not check for an existing key:', JSON.stringify(check.errors));
  if (check.data.viewer.liveSecretKey) {
    return console.log('RESULT: an API key already exists, so a new one was NOT created. Use the key you saved earlier:', check.data.viewer.liveSecretKey);
  }

  const created = await gql('mutation { regenerateSecretKey { key } }');
  console.log('RESULT:', created.data?.regenerateSecretKey?.key ?? JSON.stringify(created.errors));
})();
```

## What the script does

- Reads the Auth0 `id_token` that eonnext.com keeps in `localStorage` (keys that
  start with `@@auth0spajs@@`) after you log in. Nothing is sent anywhere except
  E.ON's own API.
- Asks E.ON whether your account already has a key (`viewer { liveSecretKey }`).
  - If one exists, it prints that key and **stops**. It does not create a new key,
    because regenerating would invalidate the old one and break anything already
    using it.
  - If none exists, it creates one with `mutation { regenerateSecretKey { key } }`
    and prints it.

## Re-authentication and rotation

- The key is long-lived. When Home Assistant asks you to re-authenticate (for
  example after the refresh token expires), you can usually leave the field blank
  to reuse the saved key, or paste the same key again.
- If you think the key has leaked, regenerate it: run only
  `mutation { regenerateSecretKey { key } }` with the same approach, then
  re-authenticate the integration with the new key. The old key stops working.

## Credits

This approach and script are based on the API-key work in
[TobySmithRM/eon-next-v2](https://github.com/TobySmithRM/eon-next-v2) (MIT).
