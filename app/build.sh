#!/usr/bin/env bash
# 构建 shudong-xray APK。
#
# 依赖：
#   JDK 17（javac / java / keytool）
#   Android SDK：build-tools（aapt2 / zipalign / apksigner.jar）+ platform（android.jar）
#
# 用法：
#   export ANDROID_JAR=$HOME/Android/Sdk/platforms/android-34/android.jar
#   export BUILD_TOOLS=$HOME/Android/Sdk/build-tools/34.0.0
#   export KS_PATH=~/my-release.jks
#   export KS_PASS=******   KS_ALIAS=shudong   KS_KEY_PASS=******
#   bash build.sh
#
# 不想用 keystore 就先去掉最后一步，用 debug key 签。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$SRC/build}"
VERSION_CODE="${VERSION_CODE:-4}"
VERSION_NAME="${VERSION_NAME:-1.2.1}"
MIN_SDK="${MIN_SDK:-23}"
TARGET_SDK="${TARGET_SDK:-34}"

: "${ANDROID_JAR:?需要 ANDROID_JAR（…/platforms/android-34/android.jar）}"
: "${BUILD_TOOLS:?需要 BUILD_TOOLS（…/build-tools/34.0.0）}"

rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/dex" "$SRC/dist"

echo "[1/6] aapt2 compile"
"$BUILD_TOOLS/aapt2" compile --dir "$SRC/res" -o "$OUT/res.zip"

echo "[2/6] aapt2 link"
"$BUILD_TOOLS/aapt2" link -o "$OUT/unsigned.apk" \
  -I "$ANDROID_JAR" \
  --manifest "$SRC/AndroidManifest.xml" \
  -A "$SRC/assets" \
  --min-sdk-version "$MIN_SDK" --target-sdk-version "$TARGET_SDK" \
  --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
  "$OUT/res.zip"

echo "[3/6] javac"
javac --release 8 -nowarn -encoding UTF-8 -classpath "$ANDROID_JAR" \
  -d "$OUT/classes" "$SRC"/java/com/shudong/xray/*.java

echo "[4/6] d8"
java -cp "$BUILD_TOOLS/lib/d8.jar" com.android.tools.r8.D8 \
  --lib "$ANDROID_JAR" --min-api "$MIN_SDK" --release --output "$OUT/dex" \
  $(find "$OUT/classes" -name "*.class")

echo "[5/6] package + zipalign"
cp "$OUT/unsigned.apk" "$OUT/withdex.apk"
python - "$OUT/withdex.apk" "$OUT/dex/classes.dex" <<'PY'
import sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
z = zipfile.ZipFile(apk, 'a', zipfile.ZIP_DEFLATED)
z.write(dex, 'classes.dex')
z.close()
PY
"$BUILD_TOOLS/zipalign" -f 4 "$OUT/withdex.apk" "$OUT/aligned.apk"

APK="$SRC/dist/shudong-xray-$VERSION_NAME.apk"

echo "[6/6] sign"
if [ -n "${KS_PATH:-}" ] && [ -n "${KS_PASS:-}" ]; then
  java -jar "$BUILD_TOOLS/lib/apksigner.jar" sign \
    --ks "$KS_PATH" --ks-pass "pass:$KS_PASS" \
    --ks-key-alias "${KS_ALIAS:-shudong}" \
    --key-pass "pass:${KS_KEY_PASS:-$KS_PASS}" \
    --out "$APK" "$OUT/aligned.apk"
else
  echo "  （没有 KS_PATH，产出未签名包 build/aligned.apk）"
  cp "$OUT/aligned.apk" "$APK"
fi

echo "OK: $APK"
[ -f "$APK" ] && ls -lh "$APK"
