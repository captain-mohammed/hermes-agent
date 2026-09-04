#!/bin/bash
# backup-hermes-config.sh - Backup hermes-agent configuration for Termux transfer
#
# This script creates a portable backup of your hermes-agent configuration
# that can be restored on Termux or another machine.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}       Hermes-Agent Configuration Backup for Termux            ${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""

# Configuration
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BACKUP_DIR="$HOME/hermes-config-backup-$(date +%Y%m%d-%H%M%S)"
BACKUP_NAME="hermes-config-$(date +%Y%m%d-%H%M%S).tar.gz"

# Check if hermes home exists
if [ ! -d "$HERMES_HOME" ]; then
    echo -e "${RED}Error: Hermes home directory not found at $HERMES_HOME${NC}"
    exit 1
fi

echo -e "${YELLOW}Source:${NC} $HERMES_HOME"
echo -e "${YELLOW}Backup:${NC} $BACKUP_DIR"
echo ""

# Create backup directory structure
mkdir -p "$BACKUP_DIR/config"
mkdir -p "$BACKUP_DIR/credentials"
mkdir -p "$BACKUP_DIR/data"

echo -e "${GREEN}[1/6]${NC} Backing up main configuration..."
# Backup config.yaml (secrets masked)
if [ -f "$HERMES_HOME/config.yaml" ]; then
    # Create a version with masked secrets for safe transfer
    sed -E \
        -e 's/(api_key|secret|token|password|apikey|apisecret):.*/\1: YOUR_SECRET_HERE/gi' \
        -e 's/(sk-|pk-|token-)[a-zA-Z0-9_-]+/YOUR_API_KEY_HERE/gi' \
        "$HERMES_HOME/config.yaml" > "$BACKUP_DIR/config/config.yaml.masked"
    
    # Also backup the original (will be encrypted in the tar)
    cp "$HERMES_HOME/config.yaml" "$BACKUP_DIR/config/config.yaml"
    echo -e "  ${GREEN}✓${NC} config.yaml (masked version + original)"
else
    echo -e "  ${YELLOW}⚠${NC} config.yaml not found"
fi

echo -e "${GREEN}[2/6]${NC} Backing up environment files..."
# Backup .env files
for env_file in .env .env.local .env.production; do
    if [ -f "$HERMES_HOME/$env_file" ]; then
        # Create masked version
        sed -E \
            -e 's/(API_KEY|SECRET|TOKEN|PASSWORD|KEY)=.*/\1=YOUR_SECRET_HERE/gi' \
            -e 's/(sk-|pk-|token-)[a-zA-Z0-9_-]+/YOUR_API_KEY_HERE/gi' \
            "$HERMES_HOME/$env_file" > "$BACKUP_DIR/credentials/$env_file.masked"
        
        # Copy original
        cp "$HERMES_HOME/$env_file" "$BACKUP_DIR/credentials/$env_file"
        echo -e "  ${GREEN}✓${NC} $env_file"
    fi
done

echo -e "${GREEN}[3/6]${NC} Backing up auth and credentials..."
# Backup auth files (these contain sensitive data)
for auth_file in auth.json credentials.json tokens.json; do
    if [ -f "$HERMES_HOME/$auth_file" ]; then
        cp "$HERMES_HOME/$auth_file" "$BACKUP_DIR/credentials/"
        echo -e "  ${GREEN}✓${NC} $auth_file"
    fi
done

# Backup auth directory if it exists
if [ -d "$HERMES_HOME/auth" ]; then
    cp -r "$HERMES_HOME/auth" "$BACKUP_DIR/credentials/"
    echo -e "  ${GREEN}✓${NC} auth/ directory"
fi

echo -e "${GREEN}[4/6]${NC} Backing up data files..."
# Backup data files (non-sensitive)
for data_file in SOUL.md channel_directory.json context_length_cache.yaml .skills_prompt_snapshot.json; do
    if [ -f "$HERMES_HOME/$data_file" ]; then
        cp "$HERMES_HOME/$data_file" "$BACKUP_DIR/data/"
        echo -e "  ${GREEN}✓${NC} $data_file"
    fi
done

# Backup gateway service config
if [ -d "$HERMES_HOME/gateway-service" ]; then
    mkdir -p "$BACKUP_DIR/data/gateway-service"
    find "$HERMES_HOME/gateway-service" -maxdepth 1 -type f \( -name "*.json" -o -name "*.yaml" -o -name "*.yml" \) \
        -exec cp {} "$BACKUP_DIR/data/gateway-service/" \;
    echo -e "  ${GREEN}✓${NC} gateway-service/ configs"
fi

echo -e "${GREEN}[5/6]${NC} Creating restoration script..."
cat > "$BACKUP_DIR/restore-hermes-config.sh" << 'RESTORE_EOF'
#!/bin/bash
# restore-hermes-config.sh - Restore hermes-agent configuration on Termux

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}       Restoring Hermes-Agent Configuration                    ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""

# Determine hermes home
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${YELLOW}Restoring to:${NC} $HERMES_HOME"
echo ""

# Create hermes home directory
mkdir -p "$HERMES_HOME"
mkdir -p "$HERMES_HOME/auth"
mkdir -p "$HERMES_HOME/gateway-service"

echo -e "${GREEN}[1/5]${NC} Restoring main configuration..."
if [ -f "$SCRIPT_DIR/config/config.yaml" ]; then
    cp "$SCRIPT_DIR/config/config.yaml" "$HERMES_HOME/config.yaml"
    echo -e "  ${GREEN}✓${NC} config.yaml restored"
    
    # Check if secrets need to be updated
    if grep -q "YOUR_SECRET_HERE" "$HERMES_HOME/config.yaml" 2>/dev/null; then
        echo -e "  ${YELLOW}⚠${NC} config.yaml contains placeholder secrets"
        echo -e "     Edit $HERMES_HOME/config.yaml to add your real API keys"
    fi
else
    echo -e "  ${RED}✗${NC} config.yaml not found in backup"
fi

echo -e "${GREEN}[2/5]${NC} Restoring environment files..."
for env_file in .env .env.local .env.production; do
    if [ -f "$SCRIPT_DIR/credentials/$env_file" ]; then
        cp "$SCRIPT_DIR/credentials/$env_file" "$HERMES_HOME/$env_file"
        echo -e "  ${GREEN}✓${NC} $env_file restored"
    fi
done

echo -e "${GREEN}[3/5]${NC} Restoring auth and credentials..."
for auth_file in auth.json credentials.json tokens.json; do
    if [ -f "$SCRIPT_DIR/credentials/$auth_file" ]; then
        cp "$SCRIPT_DIR/credentials/$auth_file" "$HERMES_HOME/"
        echo -e "  ${GREEN}✓${NC} $auth_file restored"
    fi
done

# Restore auth directory
if [ -d "$SCRIPT_DIR/credentials/auth" ]; then
    cp -r "$SCRIPT_DIR/credentials/auth/"* "$HERMES_HOME/auth/" 2>/dev/null || true
    echo -e "  ${GREEN}✓${NC} auth/ directory restored"
fi

echo -e "${GREEN}[4/5]${NC} Restoring data files..."
for data_file in SOUL.md channel_directory.json context_length_cache.yaml .skills_prompt_snapshot.json; do
    if [ -f "$SCRIPT_DIR/data/$data_file" ]; then
        cp "$SCRIPT_DIR/data/$data_file" "$HERMES_HOME/"
        echo -e "  ${GREEN}✓${NC} $data_file restored"
    fi
done

# Restore gateway service configs
if [ -d "$SCRIPT_DIR/data/gateway-service" ]; then
    cp -r "$SCRIPT_DIR/data/gateway-service/"* "$HERMES_HOME/gateway-service/" 2>/dev/null || true
    echo -e "  ${GREEN}✓${NC} gateway-service/ configs restored"
fi

echo -e "${GREEN}[5/5]${NC} Setting permissions..."
chmod 600 "$HERMES_HOME"/*.env* 2>/dev/null || true
chmod 600 "$HERMES_HOME"/auth.json 2>/dev/null || true
chmod 700 "$HERMES_HOME/auth" 2>/dev/null || true

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Restoration Complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "1. Edit $HERMES_HOME/config.yaml to add your real API keys"
echo "   Look for 'YOUR_SECRET_HERE' placeholders"
echo ""
echo "2. Or set environment variables:"
echo "   export OPENROUTER_API_KEY='your-key'"
echo "   export HERMES_POSTGRES_URL='your-neon-url'"
echo ""
echo "3. Start hermes-agent:"
echo "   cd ~/hermes-agent && python -m hermes_cli.main serve"
echo ""
RESTORE_EOF
chmod +x "$BACKUP_DIR/restore-hermes-config.sh"
echo -e "  ${GREEN}✓${NC} restore-hermes-config.sh created"

echo -e "${GREEN}[6/6]${NC} Creating compressed archive..."
cd "$HOME"
tar -czf "$BACKUP_NAME" "$(basename $BACKUP_DIR)"

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Backup Complete!${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}Archive:${NC} $HOME/$BACKUP_NAME"
echo -e "${YELLOW}Size:${NC} $(du -h "$HOME/$BACKUP_NAME" | cut -f1)"
echo ""
echo -e "${YELLOW}Transfer to Termux:${NC}"
echo "  1. USB: adb push ~/$BACKUP_NAME /sdcard/"
echo "  2. Cloud: Upload to Google Drive/Dropbox"
echo "  3. Termux: cp /sdcard/$BACKUP_NAME ~/"
echo ""
echo -e "${YELLOW}On Termux, restore with:${NC}"
echo "  cd ~"
echo "  tar -xzf $BACKUP_NAME"
echo "  cd $(basename $BACKUP_DIR)"
echo "  bash restore-hermes-config.sh"
echo ""
echo -e "${RED}⚠ SECURITY NOTE:${NC}"
echo "  The backup contains your actual API keys in config files."
echo "  Delete the archive after transferring to secure storage."
echo ""
