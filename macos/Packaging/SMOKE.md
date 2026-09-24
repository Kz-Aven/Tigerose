# Colleague smoke checklist (v0 unsigned)

完整用例见 **[docs/QA-checklist.md](../../docs/QA-checklist.md)**。  
打 dmg 步骤见 **[docs/macos-dmg.md](../../docs/macos-dmg.md)**。

1. Mac: Apple Silicon, macOS 14+
2. If Gatekeeper blocks: right-click App → Open → Open
3. First launch: model + base URL → API Key → pick workspace folder
4. Create or open an assistant → send a message
5. Create a project group with members → send `@Name …`
6. Settings (gear) → add/remove a model profile
7. Quit App → confirm no leftover `uvicorn` / `python` for Tigerose (`ps aux | grep uvicorn`)
8. Re-open → should skip onboarding and restore lists

Dev build (this machine):

```bash
./script/build_and_run.sh
```

Colleague package:

```bash
./script/build_and_run.sh --build
# put python-build-standalone arm64 at macos/Packaging/.cache/python
./macos/Packaging/bundle_runtime.sh dist/Tigerose.app
hdiutil create -volname "Tigerose" -srcfolder dist/Tigerose.app \
  -ov -format UDZO dist/Tigerose-0.1.7-arm64.dmg
```
