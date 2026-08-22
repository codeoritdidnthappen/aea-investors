<?php

/**
 * TICK-049 live probe: prove a partial, address-only demographics write against a real
 * OpenEMR (not a stub), and read the stored row back column by column.
 *
 * `ai_server/tests/` cannot do this -- it has no OpenEMR and CI starts no container, the
 * same constraint `scripts/probe_assessment_draft.py` and `evidence/TICK-017` document.
 * This script therefore drives the real `PatientDemographicsUpdateService` inside the
 * running container, against the real `PatientService`, the real `PatientValidator` and
 * the real MariaDB, and asserts on `patient_data` itself rather than on a request body.
 *
 * It exercises the service directly rather than over HTTP: the HTTP + OAuth-scope layer
 * above it (`PatientDemographicsController`, `AuthorizationListener`, the token-derived
 * `getPatientUUIDString()`) is unchanged by TICK-049 and was already proved end to end
 * through the real chat UI in `evidence/TICK-042/DEMOGRAPHICS_WRITE_ROUTE_EVIDENCE.md`.
 *
 * The probe creates its own synthetic patient through OpenEMR's own `PatientService`
 * and deletes that row again at the end, so it leaves no residue and never touches a
 * patient any other evidence file refers to.
 *
 * Run (from the repo root, with `deploy/local` up):
 *
 *   docker cp scripts/probe_demographics_address.php local-openemr-1:/tmp/probe.php
 *   docker exec local-openemr-1 chmod 644 /tmp/probe.php
 *   docker exec local-openemr-1 su -s /bin/sh apache -c \
 *     'cd /var/www/localhost/htdocs/openemr && php /tmp/probe.php'
 *
 * OpenEMR refuses to bootstrap its CLI as root (`RootCliGuard::assertNotRoot`), hence
 * `su -s /bin/sh apache`. Set `TICK049_SERVICE_PATH` to point at a service file other
 * than the bind-mounted module copy (e.g. when probing a change from a git worktree that
 * is not the mounted checkout).
 */

$_SERVER['HTTP_HOST'] = 'emr.localhost';
$_GET['site'] = 'default';
$ignoreAuth = true;
$sessionAllowWrite = true;

require_once('/var/www/localhost/htdocs/openemr/interface/globals.php');
require_once(getenv('TICK049_SERVICE_PATH') ?: '/var/www/localhost/htdocs/openemr/interface/'
    . 'modules/custom_modules/aeai-portal-chat/src/Service/PatientDemographicsUpdateService.php');

use AeaiPortalChat\Service\PatientDemographicsUpdateService;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\PatientService;

const COLUMNS = ['pid', 'fname', 'lname', 'DOB', 'street', 'street_line_2', 'city', 'state', 'postal_code'];

$failures = 0;

function out(string $label, $value): void
{
    echo "$label: $value\n";
}

function check(string $label, bool $passed): void
{
    global $failures;
    if (!$passed) {
        $failures++;
    }
    echo ($passed ? 'PASS' : 'FAIL') . " - $label\n";
}

function row(string $uuid): array
{
    return sqlQuery(
        'SELECT ' . implode(', ', COLUMNS) . ' FROM patient_data WHERE uuid = ?',
        [UuidRegistry::uuidToBytes($uuid)]
    ) ?: [];
}

function dumpRow(string $label, array $r): void
{
    echo "--- $label\n";
    foreach (COLUMNS as $column) {
        printf("    %-14s = %s\n", $column, var_export($r[$column] ?? null, true));
    }
}

function call(?string $uuid, array $body): array
{
    $response = (new PatientDemographicsUpdateService())->update($uuid, $body);
    return [$response->getStatusCode(), $response->getContent()];
}

// Fixture: a synthetic patient with a name, a date of birth, and the pre-TICK-049
// flattened single-line address, created through OpenEMR's own service layer.
$created = (new PatientService())->insert([
    'fname' => 'Tick049',
    'lname' => 'Probesubject',
    'sex' => 'Female',
    'DOB' => '1988-03-09',
    'street' => '1 Old Street, Oldtown, IL 60601',
]);
if (!$created->isValid() || $created->getInternalErrors()) {
    echo "FIXTURE FAILED\n";
    var_dump($created->getValidationMessages(), $created->getInternalErrors());
    exit(1);
}
$uuid = $created->getData()[0]['uuid'];
out('probe patient uuid', $uuid);
dumpRow('0. as created: flattened street, empty city/state/postal_code', row($uuid));

// AC1 + AC2: an address-only write updates every address column and leaves name/DOB alone.
[$status, $body] = call($uuid, [
    'street' => '42 Oak St',
    'street_line_2' => 'Apt 4B',
    'city' => 'Springfield',
    'state' => 'IL',
    'postal_code' => '62704',
]);
out('1. address-only PUT', "$status $body");
$after = row($uuid);
dumpRow('1. after the address-only write', $after);
check('1. address-only write accepted', $status === 200);
check('1. given name untouched', $after['fname'] === 'Tick049');
check('1. family name untouched', $after['lname'] === 'Probesubject');
check('1. date of birth untouched', $after['DOB'] === '1988-03-09');
check('1. street holds line 1 only', $after['street'] === '42 Oak St');
check('1. street_line_2 in its own column', $after['street_line_2'] === 'Apt 4B');
check('1. city in its own column', $after['city'] === 'Springfield');
check('1. state in its own column', $after['state'] === 'IL');
check('1. postal_code in its own column', $after['postal_code'] === '62704');
check('1. nothing was concatenated into street', !str_contains($after['street'], ','));

// AC3: a request with no recognised field at all is refused, not a successful no-op.
[$status, $body] = call($uuid, []);
out('2. empty body', "$status $body");
check('2. empty body refused', $status === 400);
[$status, $body] = call($uuid, ['nickname' => 'Ticky']);
out('2b. unrecognised field only', "$status $body");
check('2b. unrecognised field refused, not silently dropped', $status === 400);
check('2. refused requests changed nothing', row($uuid) == $after);

// AC4: the onboarding completion body -- all four fields plus a structured address.
[$status, $body] = call($uuid, [
    'fname' => 'Avery',
    'lname' => 'Van Der Berg',
    'DOB' => '1990-01-01',
    'street' => '100 Maple Avenue',
    'street_line_2' => '',
    'city' => 'Springfield',
    'state' => 'IL',
    'postal_code' => '62704',
]);
out('3. full onboarding PUT', "$status $body");
$after = row($uuid);
dumpRow('3. after the onboarding completion write', $after);
check('3. onboarding completion write accepted', $status === 200);
check('3. name written', $after['fname'] === 'Avery' && $after['lname'] === 'Van Der Berg');
check('3. date of birth written', $after['DOB'] === '1990-01-01');
check('3. address stored structured, not flattened', $after['street'] === '100 Maple Avenue'
    && $after['city'] === 'Springfield' && $after['state'] === 'IL' && $after['postal_code'] === '62704');
check('3. the previous apartment line was cleared, not left stranded', $after['street_line_2'] === '');

// A second address-only write over a full record: name and DOB survive.
[$status, $body] = call($uuid, [
    'street' => '7 Birch Ln',
    'street_line_2' => '',
    'city' => 'Peoria',
    'state' => 'IL',
    'postal_code' => '61602',
]);
out('4. second address-only PUT', "$status $body");
$after = row($uuid);
dumpRow('4. after the second address-only write', $after);
check('4. name and date of birth survive an address-only write', $after['fname'] === 'Avery'
    && $after['lname'] === 'Van Der Berg' && $after['DOB'] === '1990-01-01');
check('4. new address stored', $after['street'] === '7 Birch Ln' && $after['city'] === 'Peoria'
    && $after['postal_code'] === '61602');

// An empty value is refused for every field except the clearable second street line.
foreach (['fname', 'lname', 'DOB', 'street', 'city', 'state', 'postal_code'] as $field) {
    [$status, $body] = call($uuid, [$field => '']);
    out("5. empty '$field'", "$status $body");
    check("5. an empty '$field' cannot blank the column", $status === 400);
}

// The token-derived patient binding is still enforced.
[$status, $body] = call(null, ['city' => 'Springfield']);
out('6. no bound patient', "$status $body");
check('6. a request with no token-bound patient is refused', $status === 401);

// Cleanup: remove only the synthetic row this probe created.
sqlStatement('DELETE FROM patient_data WHERE uuid = ?', [UuidRegistry::uuidToBytes($uuid)]);
check('cleanup: the probe patient row is gone', row($uuid) === []);

echo $failures === 0 ? "\nALL CHECKS PASSED\n" : "\n$failures CHECK(S) FAILED\n";
exit($failures === 0 ? 0 : 1);
