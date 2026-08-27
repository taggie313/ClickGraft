/* Satisfies a symbol HP's arm64 build references but nothing provides.
 *
 * DjCoreServicesNative-Electron.node has an undefined flat-namespace reference
 * to png_init_filter_functions_neon. HP never trips over it because they ship
 * an Intel Electron and never load the arm64 slice; grafting an arm64 runtime
 * makes it live, and the call lands on a null pointer.
 *
 * A no-op is the CORRECT behaviour, not a fudge. In libpng the caller installs
 * the portable C filter implementations first and only then calls this to
 * override them with NEON versions. Doing nothing leaves the C paths in place:
 * PNG decoding is correct, just without the NEON acceleration it never had
 * here anyway, because the symbol was never resolvable.
 */
__attribute__((visibility("default")))
void png_init_filter_functions_neon(void *pp, unsigned int bpp)
{
    (void)pp;
    (void)bpp;
}
