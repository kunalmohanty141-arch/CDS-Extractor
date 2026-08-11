// Clean single-table view over a customer table.
@EndUserText.label: 'Custom order extraction'
@Analytics: { dataCategory: #FACT,
  dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_CUSTORDER
  as select from zcustorder
{
  key orderid   as OrderId,
      customer  as Customer,
      orderdate as OrderDate,
      amount    as Amount,
      currency  as Currency
}
