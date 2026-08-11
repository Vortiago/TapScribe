using Xunit;

// Every test in this assembly builds WinForms components on its own STA thread. Running
// classes in parallel would mean several such apartments at once, each creating and tearing
// down UI objects, for no gain — the whole assembly is a few seconds of mostly-waiting. One
// at a time keeps the run deterministic and keeps a failure attributable to the test that
// caused it, which matters more than usual here: this project can only be observed through
// CI, so an interleaved failure is one nobody can reproduce.
[assembly: CollectionBehavior(DisableTestParallelization = true)]
