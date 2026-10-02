#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
sources=(
  BrainRuntime.swift APIModel.swift NativeViews.swift NativeHealthFeature.swift
  NativeHealthHistoryFeature.swift NativeRuntimeHealthFeature.swift NativeMemoryFeature.swift
  NativeOversightFeature.swift NativeConversationFeature.swift NativeProvidersFeature.swift
  NativeConfigurationFeature.swift NativeAttachmentFeature.swift NativeOperationsFeature.swift
  NativeSecurityFeature.swift NativeVaultFeature.swift NativeConnectionsFeature.swift
  NativeHardwareFeature.swift NativeOnboardingSetupFeature.swift NativeVoiceFeature.swift
  NativeIdentityFeature.swift NativeCapabilitiesFeature.swift NativeWorkflowFeature.swift
  NativeChatToolsFeature.swift NativeChatTurnFeature.swift NativeVoiceConfigurationFeature.swift NativeIntegrationFeature.swift
  NativeRichChatFeature.swift NativeAmbientFeature.swift NativeAgentFeature.swift
  NativeKnowledgeFeature.swift NativeMemoryContextFeature.swift NativeAutomationFeature.swift
  NativeAppSurfaceFeature.swift NativeAppConfirmationFeature.swift NativeProviderRoutingFeature.swift
  NativeSurfaceUpdateFeature.swift NativeSessionRecoveryFeature.swift NativeDesktopExperience.swift
  NativeDesktopHost.swift NativeRichText.swift NativePlainTextEditor.swift NativeReviewSummary.swift
  NativeErrorPresentation.swift NativeApp.swift
)
compiler=(xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0
  -module-cache-path /private/tmp/theora-native-swift-cache -parse-as-library)
if [[ "${1:-}" == "--typecheck" && $# -eq 1 ]]; then
  "${compiler[@]}" -typecheck "${sources[@]}"
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo 'Usage: bash build.sh [--typecheck]' >&2
  exit 64
fi
mkdir -p build
"${compiler[@]}" -O "${sources[@]}" -o build/feral-native
python3 assemble.py
codesign --force --deep --sign - "build/FERAL Native Preview.app"
codesign --verify --deep --strict "build/FERAL Native Preview.app"
