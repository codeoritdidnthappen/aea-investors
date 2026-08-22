<?php
require '/var/www/localhost/htdocs/openemr/vendor/autoload.php';
$dir = '/var/www/localhost/htdocs/openemr/templates';
$loader = new \Twig\Loader\FilesystemLoader([$dir, '/tmp']);
$env = new \Twig\Environment($loader, ['cache' => false, 'debug' => true]);
$env->registerUndefinedFilterCallback(function ($name) {
    return new \Twig\TwigFilter($name, function ($v = null) { return $v; });
});
$env->registerUndefinedFunctionCallback(function ($name) {
    return new \Twig\TwigFunction($name, function () { return ''; });
});
foreach (['tick046-login.html.twig', 'tick046-scope.html.twig'] as $t) {
    try {
        $src = new \Twig\Source(file_get_contents('/tmp/' . $t), $t);
        $env->parse($env->tokenize($src));
        // full compile, which also pulls in the extended base template
        $env->load($t);
        echo "OK      $t (tokenize + parse + compile, incl. oauth2-base.html.twig)\n";
    } catch (\Throwable $e) {
        echo "FAIL    $t: " . get_class($e) . ': ' . $e->getMessage() . "\n";
    }
}
