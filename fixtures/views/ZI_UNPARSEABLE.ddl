// UNPARSEABLE — an unterminated string literal. Without it the rest of the
// file would be swallowed into the literal and a GROUP BY could hide inside.
// The correct output is UNPARSEABLE, not PASS.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_UNPARSEABLE
  as select from zcustorder
{
  key orderid  as OrderId,
      'never closed as Broken,
      customer as Customer
}
group by customer
