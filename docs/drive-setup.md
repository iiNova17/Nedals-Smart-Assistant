# Connect Google Drive

The Gemini API key does not authorize Google Drive. This assistant uses a separate Google OAuth login. Use the Google account that can edit the configured project folder.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and select the same project used for the Gemini API key. This setup does not require enabling billing.
2. Open **APIs & Services → Library**, find **Google Drive API**, and enable it.
3. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview). If this is the first OAuth client, select **Get started**. Set the app name to **Nedal’s Smart Assistant**, your email for support/contact, and **External** for the audience when using a personal Google account.
4. In **Audience**, leave the app in **Testing** initially and add your own Google email under **Test users**. The team members do not need OAuth clients; the backend will use this one authorized account.
5. In **Data Access**, add `https://www.googleapis.com/auth/drive`. This permits reading existing project files and creating uploads. Google does not provide an OAuth scope restricted to one arbitrary existing folder. The future Drive adapter must enforce the configured project-folder boundary on every operation. `drive.file` alone does not grant recursive access to existing folder contents.
6. In **Clients**, choose **Create client → Desktop app**, name it **Nedal Assistant Local Setup**, and download its JSON.
7. Save the downloaded file as `secrets/google-oauth-client.json` in this project. It and the eventual refresh token are excluded from Git and Docker images. Do not paste their contents into chat.
8. From the project directory, run:

   ```powershell
   .\.venv\Scripts\python.exe -m app.drive_auth
   ```

   Sign in with the account that can edit the folder and review Google's permission screen. The helper uses a local loopback callback, checks that the configured ID is a readable/writable folder, and lists an item count. It does not upload, modify or index files.
9. To verify a saved login without opening a new sign-in flow:

   ```powershell
   .\.venv\Scripts\python.exe -m app.drive_auth --check
   ```

External OAuth apps in **Testing** generally receive refresh tokens that expire after seven days when using Drive scopes. Before 24/7 deployment, switch the personal-use app to **In production** and authorize again. Personal-use exceptions may allow an unverified app with a warning and user cap; publishing status does not by itself mean Google has verified the app. Follow any verification requirements shown for your actual project rather than assuming approval.

A folder link grants neither authentication nor upload permission. Keep project sharing appropriate for the team; it does not need to be public. A service account is not the default for uploading into personal My Drive because it does not have its own user storage quota.

References: [Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [Refresh token expiry](https://developers.google.com/identity/protocols/oauth2#expiration), [Personal-use verification exceptions](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification).
