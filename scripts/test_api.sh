#!/bin/bash
#
# TeeTime API Test Script
# Tests all API endpoints against the deployed application
#
# Usage:
#   ./scripts/test_api.sh                    # Uses default production URL
#   ./scripts/test_api.sh http://localhost:8080  # Uses custom URL
#
# Exit codes:
#   0 - All tests passed
#   1 - One or more tests failed

# Don't use set -e as arithmetic operations can return non-zero

# Configuration
BASE_URL="${1:-https://teetime-746475271596.us-central1.run.app}"
# A fictional number in the 555-01xx range reserved for examples. Real numbers
# must never be written into this public repository (CLAUDE.md).
TEST_PHONE="+15555550100"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
PASSED=0
FAILED=0
SKIPPED=0

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

print_test() {
    echo -e "\n${YELLOW}TEST:${NC} $1"
}

print_pass() {
    echo -e "${GREEN}PASS:${NC} $1"
    ((PASSED++))
}

print_fail() {
    echo -e "${RED}FAIL:${NC} $1"
    ((FAILED++))
}

print_skip() {
    echo -e "${YELLOW}SKIP:${NC} $1"
    ((SKIPPED++))
}

print_info() {
    echo -e "${BLUE}INFO:${NC} $1"
}

# Test helper function
# Usage: test_endpoint "Test Name" "HTTP_METHOD" "endpoint" "expected_status" ["request_body"]
test_endpoint() {
    local test_name="$1"
    local method="$2"
    local endpoint="$3"
    local expected_status="$4"
    local body="$5"
    
    print_test "$test_name"
    
    local url="${BASE_URL}${endpoint}"
    local response
    local http_code
    
    if [ -n "$body" ]; then
        response=$(curl -sL -w "\n%{http_code}" -X "$method" "$url" \
            -H "Content-Type: application/json" \
            -d "$body" 2>/dev/null)
    else
        response=$(curl -sL -w "\n%{http_code}" -X "$method" "$url" 2>/dev/null)
    fi
    
    http_code=$(echo "$response" | tail -n1)
    body_response=$(echo "$response" | sed '$d')
    
    if [ "$http_code" = "$expected_status" ]; then
        print_pass "Got expected status $http_code"
        echo "Response: $body_response"
        echo "$body_response"
    else
        print_fail "Expected status $expected_status, got $http_code"
        echo "Response: $body_response"
        echo ""
    fi
}

# ============================================
# Start Tests
# ============================================

print_header "TeeTime API Test Suite"
echo "Base URL: $BASE_URL"
echo "Test Phone: $TEST_PHONE"
echo "Started at: $(date)"

# ============================================
# 1. Health & Info Endpoints
# ============================================

print_header "1. Health & Info Endpoints"

print_test "GET / - Service Info"
response=$(curl -sL -w "\n%{http_code}" "$BASE_URL/" 2>/dev/null)
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "200" ]; then
    if echo "$body" | grep -q "TeeTime"; then
        print_pass "Service info returned correctly"
        echo "Response: $body"
    else
        print_fail "Response doesn't contain expected service name"
        echo "Response: $body"
    fi
else
    print_fail "Expected status 200, got $http_code"
    echo "Response: $body"
fi

print_test "GET /health - Health Check"
response=$(curl -sL -w "\n%{http_code}" "$BASE_URL/health" 2>/dev/null)
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "200" ]; then
    if echo "$body" | grep -q "healthy"; then
        print_pass "Health check passed"
        echo "Response: $body"
    else
        print_fail "Response doesn't indicate healthy status"
        echo "Response: $body"
    fi
else
    print_fail "Expected status 200, got $http_code"
    echo "Response: $body"
fi

# ============================================
# 2. Removed Endpoints
# ============================================

print_header "2. Removed Endpoints"

# The /bookings REST router was removed in #127: it had no authentication on a
# publicly invokable service, and nothing in the bot called it.
for path in "/bookings/" "/bookings/nonexistent123"; do
    print_test "GET $path - Removed (should be 404)"
    response=$(curl -sL -w "\n%{http_code}" "$BASE_URL$path" 2>/dev/null)
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" = "404" ]; then
        print_pass "$path is not served"
    else
        print_fail "Expected status 404, got $http_code (the bookings API is reachable!)"
        echo "Response: $body"
    fi
done

# ============================================
# 3. Webhook Endpoints (Security Tests)
# ============================================

print_header "3. Webhook Endpoints (Security Tests)"

print_test "POST /webhooks/twilio/sms - Without Signature (should fail)"
response=$(curl -sL -w "\n%{http_code}" -X POST "$BASE_URL/webhooks/twilio/sms" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "From=$TEST_PHONE&To=+15555550101&Body=test" 2>/dev/null)
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "403" ]; then
    print_pass "Correctly rejected request without Twilio signature"
    echo "Response: $body"
else
    print_fail "Expected status 403, got $http_code (security issue if 200!)"
    echo "Response: $body"
fi

# ============================================
# 4. Jobs Endpoints (Security Tests)
# ============================================

print_header "4. Jobs Endpoints (Security Tests)"

print_test "POST /jobs/execute-due-bookings - Without Auth (should fail)"
response=$(curl -sL -w "\n%{http_code}" -X POST "$BASE_URL/jobs/execute-due-bookings" \
    -H "Content-Type: application/json" 2>/dev/null)
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "422" ] || [ "$http_code" = "401" ]; then
    print_pass "Correctly rejected request without authentication"
    echo "Response: $body"
else
    print_fail "Expected status 422 or 401, got $http_code (security issue if 200!)"
    echo "Response: $body"
fi

print_test "POST /jobs/execute-due-bookings - With Invalid API Key"
response=$(curl -sL -w "\n%{http_code}" -X POST "$BASE_URL/jobs/execute-due-bookings" \
    -H "Content-Type: application/json" \
    -H "X-Scheduler-API-Key: invalid-key-12345" 2>/dev/null)
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "401" ]; then
    print_pass "Correctly rejected invalid API key"
    echo "Response: $body"
else
    print_fail "Expected status 401, got $http_code"
    echo "Response: $body"
fi

# ============================================
# Summary
# ============================================

print_header "Test Summary"
echo -e "Passed:  ${GREEN}$PASSED${NC}"
echo -e "Failed:  ${RED}$FAILED${NC}"
echo -e "Skipped: ${YELLOW}$SKIPPED${NC}"
echo ""
echo "Completed at: $(date)"

if [ $FAILED -gt 0 ]; then
    echo -e "\n${RED}Some tests failed!${NC}"
    exit 1
else
    echo -e "\n${GREEN}All tests passed!${NC}"
    exit 0
fi
