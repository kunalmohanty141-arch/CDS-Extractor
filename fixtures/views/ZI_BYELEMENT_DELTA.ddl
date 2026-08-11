// R-24 — timestamp-based delta only. KBA 3514600: a view carrying only
// delta.byElement will not offer the delta option in a Replication Flow.
@Analytics: { dataCategory: #FACT,
  dataExtraction: { enabled: true,
    delta.byElement: { name: 'OrderDate', maxDelayInSeconds: 1800 } } }
define view entity ZI_BYELEMENT_DELTA
  as select from zcustorder
{
  key orderid   as OrderId,
      orderdate as OrderDate,
      amount    as Amount
}
