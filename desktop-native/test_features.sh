#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
test_cache=/private/tmp/theora-native-feature-tests-cache
all_features=(Health Memory Oversight Configuration Providers Conversation Attachment Operations Security Connections Voice Identity Capabilities Workflow ChatTools VoiceConfiguration Integration RichChat Ambient Agent Knowledge MemoryContext Automation AppSurface AppConfirmation ProviderRouting SessionRecovery SurfaceUpdate HealthHistory RuntimeHealth Vault Hardware OnboardingSetup ReviewSummary)
features=("${all_features[@]}")
if [[ $# -gt 0 ]]; then
  features=("$@")
  for requested in "${features[@]}"; do
    found=false
    for known in "${all_features[@]}"; do [[ "$requested" != "$known" ]] || found=true; done
    if [[ "$found" != true ]]; then echo "Unknown feature suite: $requested" >&2; exit 64; fi
  done
fi
for feature in "${features[@]}"; do
  test_binary="/private/tmp/theora-native-${feature}-feature-tests"
  feature_sources=(NativeRichText.swift NativePlainTextEditor.swift NativeReviewSummary.swift)
  main_source="Native${feature}Feature.swift"
  test_source="tests/Native${feature}FeatureTests.swift"
  if [[ "$feature" == "ReviewSummary" ]]; then feature_sources=(NativeRichText.swift NativePlainTextEditor.swift); main_source=NativeReviewSummary.swift; test_source=tests/NativeReviewSummaryTests.swift; fi
  if [[ "$feature" == "AppSurface" ]]; then feature_sources+=(NativeRichChatFeature.swift NativeAppConfirmationFeature.swift NativeSurfaceUpdateFeature.swift); fi
  if [[ "$feature" == "SurfaceUpdate" ]]; then feature_sources+=(NativeRichChatFeature.swift NativeAppConfirmationFeature.swift NativeAppSurfaceFeature.swift); fi
  if [[ "$feature" == "Providers" ]]; then feature_sources+=(NativeProviderRoutingFeature.swift); fi
  xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 \
    -module-cache-path "$test_cache" -parse-as-library \
    "${feature_sources[@]}" "$main_source" "$test_source" \
    -o "$test_binary"
  "$test_binary"
done
model_binary=/private/tmp/theora-native-model-wire-tests
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 \
  -module-cache-path "$test_cache" -parse-as-library \
  BrainRuntime.swift APIModel.swift NativeViews.swift NativeErrorPresentation.swift \
  NativeHealthFeature.swift NativeHealthHistoryFeature.swift NativeRuntimeHealthFeature.swift NativeMemoryFeature.swift NativeOversightFeature.swift \
  NativeConfigurationFeature.swift NativeProvidersFeature.swift NativeConversationFeature.swift \
  NativeAttachmentFeature.swift NativeRichText.swift NativePlainTextEditor.swift NativeReviewSummary.swift \
  NativeOperationsFeature.swift NativeSecurityFeature.swift NativeVaultFeature.swift NativeConnectionsFeature.swift NativeHardwareFeature.swift NativeOnboardingSetupFeature.swift NativeVoiceFeature.swift \
  NativeIdentityFeature.swift NativeCapabilitiesFeature.swift NativeWorkflowFeature.swift \
  NativeChatToolsFeature.swift NativeVoiceConfigurationFeature.swift NativeIntegrationFeature.swift NativeRichChatFeature.swift NativeAmbientFeature.swift NativeAgentFeature.swift NativeKnowledgeFeature.swift NativeMemoryContextFeature.swift NativeAutomationFeature.swift NativeAppSurfaceFeature.swift NativeAppConfirmationFeature.swift NativeProviderRoutingFeature.swift NativeSurfaceUpdateFeature.swift NativeSessionRecoveryFeature.swift NativeDesktopExperience.swift NativeDesktopHost.swift \
  NativeModelTests.swift -o "$model_binary"
"$model_binary"

desktop_binary=/private/tmp/theora-native-desktop-experience-tests
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 -module-cache-path "$test_cache" -parse-as-library NativeDesktopExperience.swift tests/NativeDesktopExperienceTests.swift -o "$desktop_binary"
"$desktop_binary"

error_binary=/private/tmp/theora-native-error-presentation-tests
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 -module-cache-path "$test_cache" -parse-as-library NativeErrorPresentation.swift tests/NativeErrorPresentationTests.swift -o "$error_binary"
"$error_binary"
